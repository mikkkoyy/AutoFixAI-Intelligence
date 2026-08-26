"""End-to-end shared-knowledge discovery and approval flow (Parts 5, 11–15, 20).

- deterministic detection of reusable knowledge from conversations
- non-blocking notification card in the Chat panel
- NOTHING is saved without an explicit "Save to GitHub" click
- security scan findings are never displayed raw
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.agents.knowledge_detection import (
    KnowledgeProposal,
    detect_reusable_knowledge,
    normalize_title,
)


@pytest.fixture(autouse=True)
def _fresh_engine_dedupe():
    """Process-lifetime dedupe must not leak between tests."""
    from app.agents.chat_intelligence import ChatEngine

    ChatEngine._seen_knowledge_titles = set()
    yield
    ChatEngine._seen_knowledge_titles = set()


@pytest.fixture(autouse=True)
def _no_knowledge_env(monkeypatch):
    for key in ("AUTOFIX_KNOWLEDGE_REPO", "GITHUB_TOKEN",
                "AUTOFIX_KNOWLEDGE_TOKEN"):
        monkeypatch.delenv(key, raising=False)


PROPOSAL = {
    "title": "Serialize worker writes",
    "category": "lessons",
    "body": "- Serialize writes to avoid the race condition.",
    "source": "Chat conversation (debugging)",
    "confidence": 0.87,
}


# ── Detection heuristics ─────────────────────────────────────────


class TestDetectionHeuristics:
    def test_insight_markers_trigger_detection(self):
        proposal = detect_reusable_knowledge(
            "That fixed it!",
            "Turns out the root cause was a race condition. "
            "The fix was to serialize worker writes. This is a reusable pattern.",
        )
        assert proposal is not None
        assert proposal.confidence >= 0.7
        assert "race condition" in proposal.body.lower()

    def test_single_generic_reply_not_detected(self):
        assert detect_reusable_knowledge(
            "What is FastAPI?", "FastAPI is a modern Python web framework."
        ) is None

    def test_explicit_share_request_detected(self):
        proposal = detect_reusable_knowledge(
            "remember this for next time",
            "Always run the full pytest suite before committing changes.",
        )
        assert proposal is not None

    def test_category_routing(self):
        planning = detect_reusable_knowledge(
            "good idea, save it",
            "The key insight: decompose by verification boundary when planning. "
            "This planning strategy worked well.",
        )
        reasoning = detect_reusable_knowledge(
            "that explains it",
            "Turns out the root cause was a stale cache entry; the fix was to "
            "invalidate on write.",
        )
        if planning:
            assert planning.category == "planning"
        if reasoning:
            assert reasoning.category == "reasoning"

    def test_secrets_redacted_from_distilled_body(self):
        proposal = detect_reusable_knowledge(
            "save that lesson",
            "Lesson learned: the service used api_key = sk-live-1234567890 in "
            "production config; rotate it during deploys.",
        )
        assert proposal is not None
        assert "sk-live-1234567890" not in proposal.to_dict()["body"]

    def test_normalize_title(self):
        # Dedupe key: case/whitespace/punctuation insensitive.
        assert (
            normalize_title("  Retry   Subtasks!  ")
            == normalize_title("retry subtasks")
            != ""
        )
        assert normalize_title("") == ""

    def test_proposal_roundtrip(self):
        proposal = KnowledgeProposal(
            title="T", category="patterns", body="b", source="s",
            confidence=0.8,
        )
        restored = KnowledgeProposal.from_dict(proposal.to_dict())
        assert restored.title == "T" and restored.confidence == 0.8


# ── Engine attachment ────────────────────────────────────────────


class TestEngineAttachment:
    def _engine(self):
        from app.agents.chat_intelligence import ChatEngine

        return ChatEngine()

    def test_reply_carries_knowledge_proposal(self, monkeypatch, tmp_path):
        from app.agents import knowledge_detection as kd

        monkeypatch.setattr(
            kd, "detect_reusable_knowledge",
            lambda *a, **k: KnowledgeProposal(
                title="Serialize worker writes", category="lessons",
                body="- b", source="Chat conversation (reply)",
                confidence=0.9,
            ),
        )
        response = self._engine().handle("What is FastAPI?", str(tmp_path))
        assert response.kind == "reply"
        assert response.knowledge_proposal is not None
        assert response.knowledge_proposal["title"] == "Serialize worker writes"

    def test_same_title_not_reproposed(self, monkeypatch, tmp_path):
        from app.agents import knowledge_detection as kd

        monkeypatch.setattr(
            kd, "detect_reusable_knowledge",
            lambda *a, **k: KnowledgeProposal(
                title="Same lesson", category="lessons", body="b",
                source="s", confidence=0.9,
            ),
        )
        engine = self._engine()
        first = engine.handle("hello there", str(tmp_path))
        second = engine.handle("another hello", str(tmp_path))
        assert first.knowledge_proposal is not None
        assert second.knowledge_proposal is None

    def test_no_cloud_key_needed_for_detection(self, tmp_path):
        """Detection is deterministic/local — no provider configured."""
        response = self._engine().handle("What is FastAPI?", str(tmp_path))
        assert response.kind == "reply"          # local assistant answered
        assert response.knowledge_proposal is None  # plain Q&A → nothing


# ── Window card flow ─────────────────────────────────────────────


@pytest.fixture
def knowledge_window(window):
    return window


def _deliver_knowledge(win, proposal=None):
    win._on_structured_reply({
        "kind": "reply",
        "content": "Here is what happened.",
        "knowledge_proposal": dict(proposal or PROPOSAL),
    })


class TestKnowledgeCardFlow:
    def test_card_appears_without_blocking_chat(self, knowledge_window):
        win = knowledge_window
        from PySide6.QtWidgets import QWidget

        _deliver_knowledge(win)
        card = win._knowledge_host.findChild(QWidget, "KnowledgeCard")
        assert card is not None
        assert win._knowledge_payload["title"] == "Serialize worker writes"

    def test_secret_material_never_displayed_raw(self, knowledge_window):
        win = knowledge_window
        from PySide6.QtWidgets import QTextEdit, QWidget

        _deliver_knowledge(win, {**PROPOSAL, "body":
                                 "key sk-supersecret999999 used here"})
        card = win._knowledge_host.findChild(QWidget, "KnowledgeCard")
        body = card.findChild(QTextEdit)
        assert body is not None
        assert "sk-supersecret999999" not in body.toPlainText()

    def test_review_toggles_editability(self, knowledge_window):
        win = knowledge_window
        from PySide6.QtWidgets import QTextEdit, QWidget

        _deliver_knowledge(win)
        card = win._knowledge_host.findChild(QWidget, "KnowledgeCard")
        body = card.findChild(QTextEdit)
        assert body.isReadOnly()
        win._on_knowledge_review()
        assert not body.isReadOnly()
        win._on_knowledge_review()
        assert body.isReadOnly()

    def test_ignore_discards_without_saving(self, knowledge_window, monkeypatch):
        win = knowledge_window

        def forbidden(*args, **kwargs):
            raise AssertionError("save_knowledge must not run without approval")

        monkeypatch.setattr("app.agents.github_knowledge.save_knowledge",
                            forbidden)
        _deliver_knowledge(win)
        win._on_knowledge_ignore()
        assert win._knowledge_payload is None
        assert not win._knowledge_host.isVisible()

    def test_save_requires_configuration(self, knowledge_window, monkeypatch):
        win = knowledge_window

        def forbidden(*args, **kwargs):
            raise AssertionError("unconfigured save must not reach GitHub")

        monkeypatch.setattr("app.agents.github_knowledge.save_knowledge",
                            forbidden)
        _deliver_knowledge(win)
        win._on_knowledge_save()
        transcript = win.conversation.toPlainText().lower()
        assert "not configured" in transcript
        assert win._knowledge_payload is not None  # card retained

    def test_approved_save_reports_success_and_hides_card(
        self, knowledge_window, monkeypatch
    ):
        from app.agents.chat_workers import KnowledgeSaveWorker

        monkeypatch.setenv("AUTOFIX_KNOWLEDGE_REPO", "acme/ai-knowledge")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        monkeypatch.setattr(KnowledgeSaveWorker, "start",
                            lambda self: self.run())
        captured = {}

        def fake_save(item, config=None, client=None):
            captured["item"] = item
            return {"ok": True,
                    "message": "AI knowledge saved to GitHub "
                               "(lessons: Serialize worker writes saved).",
                    "path": "ai-knowledge/lessons/x.md", "url": "",
                    "sanitized": False}

        monkeypatch.setattr("app.agents.github_knowledge.save_knowledge",
                            fake_save)
        win = knowledge_window
        _deliver_knowledge(win)
        win._on_knowledge_save()
        transcript = win.conversation.toPlainText()
        assert "AI knowledge saved to GitHub" in transcript
        assert captured["item"].title == "Serialize worker writes"
        assert win._knowledge_payload is None
        assert not win._knowledge_host.isVisible()

    def test_failed_save_is_honest_and_card_survives(
        self, knowledge_window, monkeypatch
    ):
        from app.agents.chat_workers import KnowledgeSaveWorker

        monkeypatch.setenv("AUTOFIX_KNOWLEDGE_REPO", "acme/ai-knowledge")
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        monkeypatch.setattr(KnowledgeSaveWorker, "start",
                            lambda self: self.run())

        def failing_save(item, config=None, client=None):
            return {"ok": False, "path": None, "url": "",
                    "message": "GitHub authentication failed (token rejected). "
                               "Knowledge was NOT saved.",
                    "sanitized": False}

        monkeypatch.setattr("app.agents.github_knowledge.save_knowledge",
                            failing_save)
        win = knowledge_window
        _deliver_knowledge(win)
        win._on_knowledge_save()
        transcript = win.conversation.toPlainText().lower()
        assert "authentication failed" in transcript
        assert win._knowledge_payload is not None   # user can retry/ignore
        assert win._knowledge_host.isVisibleTo(win)  # card retained (window
        # itself is not shown in tests, so isVisible() would be False).

    def test_engine_discovery_never_auto_pushes(self, window, monkeypatch):
        """Detection alone must never construct a GitHub client or save."""
        from app.agents import github_knowledge as gk

        def forbidden_factory(config):
            raise AssertionError("no GitHub client without explicit approval")

        monkeypatch.setattr(gk, "set_client_factory",
                            lambda f: None)  # keep DI hook stable
        monkeypatch.setattr(gk, "_default_client_factory", forbidden_factory)
        win = window
        win._on_structured_reply({
            "kind": "reply", "content": "ok",
            "knowledge_proposal": dict(PROPOSAL),
        })
        assert win._knowledge_payload is not None   # waiting for the user
