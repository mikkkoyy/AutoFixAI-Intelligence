"""AI chat mode switch and routing tests for the locked AutoFix architecture."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtCore import QObject, Signal

import app.ui.main_window as mw
from app.agents.chat_provider import (
    LocalAssistant,
    available_providers,
    converse,
)
from app.agents.chat_workers import ChatWorker, OpenCodeChatWorker
from app.agents.pipeline import PlanWorker


# ── Mode switch UI ─────────────────────────────────────────────


class TestModeSwitchUI:
    def test_default_mode_is_chat(self, window):
        assert window.current_ai_mode == "chat"
        assert window._mode_buttons["chat"].isChecked()
        assert not any(
            window._mode_buttons[key].isChecked()
            for key in ("autofix",)
        )

    def test_all_modes_present(self, window):
        assert set(window._mode_buttons) == {"chat", "autofix"}
        assert dict(mw.MainWindow.AI_MODES) == {
            "chat": "Chat",
            "autofix": "AutoFix",
        }

    def test_mode_buttons_are_checkable_and_exclusive(self, window):
        for button in window._mode_buttons.values():
            assert button.isCheckable()

        window.set_ai_mode("autofix")
        assert window._mode_buttons["autofix"].isChecked()
        assert not window._mode_buttons["chat"].isChecked()

    def test_unknown_mode_rejected(self, window):
        assert window.set_ai_mode("nope") is False
        assert window.current_ai_mode == "chat"

    def test_switching_mode_announces_in_chat(self, window):
        window.set_ai_mode("autofix")
        assert "Mode: AutoFix" in window.conversation.toPlainText()


# ── Mode 1: Chat ───────────────────────────────────────────────


class TestChatMode:
    def test_hello_does_not_invoke_planner(self, window, monkeypatch):
        created = []
        monkeypatch.setattr(PlanWorker, "__init__", lambda self, *a, **k: created.append(1))

        window.on_chat_send("hello")

        assert created == []
        # APPROVE & EXECUTE is always visible but inert without a plan —
        # normal conversation must never arm it.
        assert window.approve_button.isVisibleTo(window)
        assert not window.approve_button.isEnabled()
        assert window._pending_plan is None
        assert "Analyzing the workspace" not in window.conversation.toPlainText()
        assert "Source files:" not in window.conversation.toPlainText()
        assert "Pipeline:" not in window.conversation.toPlainText()

    def test_hello_starts_chat_worker_with_workspace(self, window, monkeypatch, tmp_path):
        captured = {}
        real_init = ChatWorker.__init__

        def fake_init(self, message, workspace, history=None, parent=None):
            real_init(self, message, workspace, history, parent)
            captured["message"] = message
            captured["workspace"] = workspace

        monkeypatch.setattr(ChatWorker, "__init__", fake_init)

        window.set_active_workspace(str(tmp_path))
        window.on_chat_send("hello")

        assert captured["message"] == "hello"
        assert captured["workspace"] == str(tmp_path)
        assert isinstance(window._chat_worker_thread, ChatWorker)

    def test_reply_is_appended(self, window):
        window.on_chat_send("hello")
        window._on_chat_reply("Hello! I'm AutoFix Assistant. How can I help?")

        text = window.conversation.toPlainText()
        assert "Hello! I'm AutoFix Assistant. How can I help?" in text

    def test_error_block_rendered(self, window):
        window._on_chat_error("connection refused")

        text = window.conversation.toPlainText()
        assert "AI Chat Error" in text
        assert "Unable to reach the configured AI provider." in text
        assert "connection refused" in text

    def test_history_recorded(self, window):
        window.on_chat_send("hi there")
        window._on_chat_reply("greeting reply")

        assert ("user", "hi there") in window._conversation_history
        assert ("assistant", "greeting reply") in window._conversation_history


# ── Mode 2: AutoFix Agent ──────────────────────────────────────


class TestAutoFixMode:
    def test_autofix_routes_to_plan_worker(self, window, monkeypatch):
        captured = {}
        real_init = PlanWorker.__init__

        def fake_init(self, description, workspace, parent=None):
            real_init(self, description, workspace, parent)
            captured["description"] = description
            captured["workspace"] = workspace

        monkeypatch.setattr(PlanWorker, "__init__", fake_init)
        window.set_ai_mode("autofix")
        window.on_chat_send("fix the login bug")

        assert captured["description"] == "fix the login bug"
        assert isinstance(window._plan_worker, PlanWorker)
        assert "Analyzing the workspace and preparing a plan…" in window.conversation.toPlainText()

    def test_plan_ready_shows_approve_button(self, window):
        window.set_ai_mode("autofix")
        window._last_request = "add a feature"
        window._on_plan_ready("1. Do A")

        assert window.approve_button.isVisibleTo(window)
        assert window.approve_button.isEnabled()
        assert window._pending_plan is not None

    def test_approve_visible_and_enabled_across_mode_switches(self, window):
        """The button must never disappear when switching modes."""
        window.set_ai_mode("autofix")
        window._last_request = "task"
        window._on_plan_ready("plan")
        assert window.approve_button.isVisibleTo(window)
        assert window.approve_button.isEnabled()

        for mode in ("chat", "autofix"):
            window.set_ai_mode(mode)
            assert window.approve_button.isVisibleTo(window), mode
            assert window.approve_button.isEnabled(), mode
        assert window._pending_plan is not None

    def test_returning_to_autofix_keeps_pending_plan_actionable(self, window):
        window.set_ai_mode("autofix")
        window._last_request = "task"
        window._on_plan_ready("plan")
        window.set_ai_mode("chat")
        window.set_ai_mode("autofix")

        assert window.approve_button.isVisibleTo(window)
        assert window.approve_button.isEnabled()
        assert window._pending_plan is not None


# ── Mode 3: OpenCode ───────────────────────────────────────────


class FakeOpenCodeWorker(QObject):
    last = None

    output_received = Signal(str)
    request_finished = Signal(bool, str)

    def __init__(self, prompt, workspace, parent=None):
        super().__init__(parent)
        self.prompt = prompt
        self.workspace = workspace
        self.started = False
        FakeOpenCodeWorker.last = self

    def isRunning(self):
        return False

    def cancel(self):
        pass

    def start(self):
        self.started = True


class TestOpenCodeMode:
    def setup_method(self):
        FakeOpenCodeWorker.last = None

    def _monkeypatch_worker(self, monkeypatch):
        monkeypatch.setattr(mw, "OpenCodeChatWorker", FakeOpenCodeWorker)

    def test_opencode_uses_active_workspace(self, window, monkeypatch, tmp_path):
        self._monkeypatch_worker(monkeypatch)
        window.set_active_workspace(str(tmp_path))
        # OpenCode is now an internal worker; invoke the handler directly.
        window._handle_opencode_message("refactor the parser module")

        worker = FakeOpenCodeWorker.last
        assert worker is not None
        assert worker.prompt == "refactor the parser module"
        assert worker.workspace == str(tmp_path)
        assert worker.started is True

    def test_opencode_follows_workspace_change(self, window, monkeypatch, tmp_path):
        self._monkeypatch_worker(monkeypatch)
        window._handle_opencode_message("first request")
        first = FakeOpenCodeWorker.last.workspace

        window.set_active_workspace(str(tmp_path))
        window._handle_opencode_message("second request")
        second = FakeOpenCodeWorker.last.workspace

        assert first != second
        assert second == str(tmp_path)

    def test_opencode_unavailable_block(self, window):
        window._on_opencode_finished(False, "not found on PATH")

        text = window.conversation.toPlainText()
        assert "OpenCode Unavailable" in text
        assert "OpenCode could not be started or reached." in text

    def test_opencode_success_appended(self, window):
        window._on_opencode_finished(True, "done refactoring")

        assert "done refactoring" in window.conversation.toPlainText()


# ── History preservation across mode switches ──────────────────


class TestHistoryPreservation:
    def test_conversation_survives_all_mode_changes(self, window):
        window.on_chat_send("hello")
        window._on_chat_reply("Hi! How can I help?")

        marker = "Hi! How can I help?"
        assert marker in window.conversation.toPlainText()

        for mode in ("autofix", "opencode", "chat"):
            window.set_ai_mode(mode)
            assert marker in window.conversation.toPlainText(), (
                f"history lost after switching to {mode}"
            )

    def test_mode_switch_does_not_clear_input_or_workers(self, window):
        window.on_chat_send("hello")
        worker_before = window._chat_worker_thread
        window.set_ai_mode("autofix")

        assert window._chat_worker_thread is worker_before


# ── Provider layer ─────────────────────────────────────────────


class TestChatProviders:
    def test_no_providers_when_env_empty(self):
        assert available_providers(env={}) == []

    def test_openai_key_detected(self):
        providers = available_providers(env={"OPENAI_API_KEY": "sk-test"})
        assert [p.name for p in providers] == ["GPT"]
        assert providers[0].kind == "openai"

    def test_anthropic_key_detected(self):
        providers = available_providers(env={"ANTHROPIC_API_KEY": "k"})
        assert providers[0].name == "Claude"
        assert providers[0].kind == "anthropic"

    def test_deterministic_autofix_provider_ignored(self):
        env = {
            "AUTOFIX_PROVIDER": "deterministic",
            "AUTOFIX_API_KEY": "sk-x",
            "AUTOFIX_BASE_URL": "https://api.openai.com/v1",
        }
        assert available_providers(env=env) == []

    def test_openai_compatible_autofix_provider_detected(self):
        env = {
            "AUTOFIX_PROVIDER": "openai-compatible",
            "AUTOFIX_API_KEY": "sk-x",
            "AUTOFIX_BASE_URL": "https://example.com/v1",
            "AUTOFIX_MODEL": "mymodel",
        }
        providers = available_providers(env=env)
        assert len(providers) == 1
        assert providers[0].model == "mymodel"
        assert providers[0].base_url == "https://example.com/v1"

    def test_converse_falls_back_to_local_assistant(self):
        reply = converse("hello", workspace=".", env={})
        assert "local" in reply.lower()
        assert "AutoFix Assistant" in reply

    def test_local_assistant_greeting(self):
        reply = LocalAssistant.respond("hello", ".")
        lowered = reply.lower()
        assert "hello" in lowered
        assert "openai_api_key" in lowered or "provider" in lowered

    def test_local_assistant_lists_workspace(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "README.md").write_text("x", encoding="utf-8")

        reply = LocalAssistant.respond("what files are in this project?", tmp_path)

        assert "[dir]" in reply
        assert "src" in reply
        assert "README.md" in reply

    def test_local_assistant_never_claims_to_be_cloud_model(self):
        for message in ("hello", "explain python", "thanks"):
            reply = LocalAssistant.respond(message, ".").lower()
            assert "i'm gpt" not in reply
            assert "i am claude" not in reply
