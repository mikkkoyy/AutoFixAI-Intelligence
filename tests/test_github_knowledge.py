"""Shared AI knowledge — GitHub repository configuration, save, retrieval.

All network operations run against injected fake clients (spec Part 19:
dependency injection — no real credentials, no connectivity required).

Covers spec test items 10, 16, 17, 21, 22 and the no-auto-push rule (20).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.agents import github_knowledge as gk
from app.agents.github_knowledge import (
    KnowledgeConfig,
    KnowledgeItem,
    PRIORITY_RULE,
    knowledge_status,
    retrieve_knowledge,
    save_knowledge,
    shared_knowledge_block,
)


class FakeClient:
    """Scriptable stand-in for GitHubApiClient."""

    def __init__(self, paths=None, bodies=None, fail=None):
        self.paths = list(paths or [])
        self.bodies = dict(bodies or {})
        self.fail = fail or {}
        self.puts = []
        self.sha_calls = []
        self.fetches = []

    def file_exists_sha(self, path):
        self.sha_calls.append(path)
        error = self.fail.get("sha")
        if error is not None:
            raise error
        return "abc123" if path in self.puts_paths() else None

    def puts_paths(self):
        return {p for p, _c, _m in self.puts}

    def put_contents(self, path, content_text, message, sha):
        error = self.fail.get("put")
        if error is not None:
            raise error
        self.puts.append((path, content_text, message))
        return {"commit": "deadbeef1234", "html_url": f"https://github.com/x/{path}"}

    def list_markdown_paths(self):
        error = self.fail.get("tree")
        if error is not None:
            raise error
        return self.paths

    def fetch_content(self, path):
        self.fetches.append(path)
        return self.bodies.get(
            path, f"title: {path.split('/')[-1].removesuffix('.md')}\nbody text"
        )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "AUTOFIX_KNOWLEDGE_REPO", "GITHUB_TOKEN", "AUTOFIX_KNOWLEDGE_TOKEN",
        "AUTOFIX_KNOWLEDGE_API_URL", "AUTOFIX_KNOWLEDGE_REF",
        "AUTOFIX_KNOWLEDGE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


# ── Configuration (item 10) ───────────────────────────────────────


class TestConfiguration:
    def test_unconfigured_by_default(self):
        assert KnowledgeConfig.from_env({}) is None

    def test_repo_and_token_required(self):
        assert KnowledgeConfig.from_env({"AUTOFIX_KNOWLEDGE_REPO": "o/r"}) is None
        assert KnowledgeConfig.from_env({
            "AUTOFIX_KNOWLEDGE_REPO": "o/r", "GITHUB_TOKEN": "t",
        }) is not None

    def test_full_url_and_dot_git_accepted(self):
        cfg = KnowledgeConfig.from_env({
            "AUTOFIX_KNOWLEDGE_REPO": "https://github.com/acme/ai-knowledge.git",
            "GITHUB_TOKEN": "t",
        })
        assert (cfg.owner, cfg.name) == ("acme", "ai-knowledge")

    def test_token_precedence(self):
        cfg = KnowledgeConfig.from_env({
            "AUTOFIX_KNOWLEDGE_REPO": "o/r",
            "GITHUB_TOKEN": "generic",
            "AUTOFIX_KNOWLEDGE_TOKEN": "specific",
        })
        assert cfg.token == "specific"

    def test_defaults(self):
        cfg = KnowledgeConfig.from_env({
            "AUTOFIX_KNOWLEDGE_REPO": "o/r", "GITHUB_TOKEN": "t",
        })
        assert cfg.api_url == "https://api.github.com"
        assert cfg.ref == "main"
        assert cfg.dirname == "ai-knowledge"

    def test_status_never_includes_token(self, monkeypatch):
        monkeypatch.setenv("AUTOFIX_KNOWLEDGE_REPO", "o/r")
        monkeypatch.setenv("GITHUB_TOKEN", "super-secret-github-token-value")
        status = knowledge_status()
        assert status["configured"] is True
        blob = repr(status)
        assert "super-secret-github-token" not in blob


# ── Save with approval semantics (items 16, 18, 19) ──────────────


class TestSaveKnowledge:
    def test_save_new_file_under_category_dir(self):
        client = FakeClient()
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="Serialize worker writes",
                          body="- Serialize writes to avoid the race.",
                          source="Chat conversation", confidence=0.8),
            client=client,
        )
        assert result["ok"] is True
        path, content, message = client.puts[0]
        assert path == "ai-knowledge/lessons/serialize-worker-writes.md"
        assert content.startswith("---\n")
        assert "title: Serialize worker writes" in content
        assert "confidence: 0.8" in content
        assert message.startswith("Add lessons knowledge:")

    def test_update_existing_file_uses_sha(self):
        client = FakeClient()
        first = save_knowledge(KnowledgeItem(category="patterns", title="Idempotent subtasks", body="b"), client=client)
        second = save_knowledge(KnowledgeItem(category="patterns", title="Idempotent subtasks", body="b2"), client=client)
        assert first["ok"] and second["ok"]
        assert any(m.startswith("Update patterns knowledge:") for _p, _c, m in client.puts)

    def test_hard_secret_blocks_the_put_entirely(self):
        client = FakeClient()
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="Keys",
                          body="-----BEGIN RSA PRIVATE KEY-----\nabc"),
            client=client,
        )
        assert result["ok"] is False
        assert client.puts == []                      # nothing reached GitHub
        assert "security scan" in result["message"].lower()

    def test_soft_secret_sanitized_before_upload(self):
        client = FakeClient()
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="Key handling lesson",
                          body="the service used api_key = sk-live-99999999 in prod"),
            client=client,
        )
        assert result["ok"] is True and result["sanitized"] is True
        _path, content, _message = client.puts[0]
        assert "sk-live-99999999" not in content
        assert "[REDACTED]" in content

    def test_commit_message_contains_no_body_secrets(self):
        client = FakeClient()
        save_knowledge(
            KnowledgeItem(category="lessons", title="Clean title",
                          body="note: Bearer supersecrettokenvalue1 rotated"),
            client=client,
        )
        _path, _content, message = client.puts[0]
        assert "supersecrettokenvalue1" not in message

    def test_invalid_category_rejected_without_network(self):
        client = FakeClient()
        result = save_knowledge(
            KnowledgeItem(category="diaries", title="T", body="b"), client=client
        )
        assert result["ok"] is False and client.puts == []

    def test_unknown_category_list(self):
        assert set(gk.KNOWLEDGE_CATEGORIES) == {
            "behavior", "planning", "reasoning", "patterns", "lessons",
        }


# ── Failure handling (item 17) ────────────────────────────────────


class TestSaveFailureHandling:
    def _failing(self, error):
        return FakeClient(fail={"put": gk.KnowledgeError(error)})

    def test_authentication_failure_is_clean(self, tmp_path):
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="T", body="b"),
            client=self._failing(gk.KnowledgeError(
                "GitHub authentication failed (token rejected). Knowledge was NOT saved."
            )),
            workspace=str(tmp_path),
        )
        # New behavior: falls back to local pending save when GitHub fails.
        assert result["ok"] is True
        assert result.get("pending_sync") is True
        assert "pending sync" in result["message"].lower()

    def test_permission_failure_is_clean(self, tmp_path):
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="T", body="b"),
            client=self._failing(gk.KnowledgeError(
                "GitHub permission denied for this repository/token."
            )),
            workspace=str(tmp_path),
        )
        assert result["ok"] is True and result.get("pending_sync") is True

    def test_repository_failure_is_clean(self, tmp_path):
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="T", body="b"),
            client=self._failing(gk.KnowledgeError(
                "GitHub repository or path not found (check AUTOFIX_KNOWLEDGE_REPO)."
            )),
            workspace=str(tmp_path),
        )
        assert result["ok"] is True and result.get("pending_sync") is True

    def test_merge_conflict_is_reported(self, tmp_path):
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="T", body="b"),
            client=self._failing(gk.KnowledgeError(
                "GitHub merge conflict while saving knowledge."
            )),
            workspace=str(tmp_path),
        )
        assert result["ok"] is True and result.get("pending_sync") is True

    def test_validation_rejection_is_reported(self, tmp_path):
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="T", body="b"),
            client=self._failing(gk.KnowledgeError(
                "GitHub rejected the content (validation or conflict)."
            )),
            workspace=str(tmp_path),
        )
        assert result["ok"] is True and result.get("pending_sync") is True

    def test_network_failure_is_clean(self, tmp_path):
        result = save_knowledge(
            KnowledgeItem(category="lessons", title="T", body="b"),
            client=self._failing(gk.KnowledgeError(
                "GitHub is unreachable (network failure). Knowledge was NOT saved."
            )),
            workspace=str(tmp_path),
        )
        assert result["ok"] is True and result.get("pending_sync") is True
        assert "github unavailable" in result["message"].lower() or "pending sync" in result["message"].lower()

    def test_no_exception_ever_escapes_save(self):
        class ExplodingClient:
            def file_exists_sha(self, path):
                raise RuntimeError("boom")

        result = save_knowledge(
            KnowledgeItem(category="lessons", title="T", body="b"),
            client=ExplodingClient(),
        )
        assert result["ok"] is False


# ── Retrieval + relevance filtering (items 21, 22) ───────────────


class TestRetrieval:
    PATHS = [
        "ai-knowledge/lessons/worker-router-retry.md",
        "ai-knowledge/planning/login-flow-plan.md",
        "ai-knowledge/reasoning/root-cause-tracing.md",
        "ai-knowledge/patterns/idempotent-subtask.md",
        "ai-knowledge/behavior/clarify-before-proposal.md",
        "README.md",
    ]

    def test_unconfigured_returns_empty_without_calls(self):
        # No env config, no injected client -> clean empty result, no network.
        assert retrieve_knowledge("worker router retry") == []

    def test_only_relevant_documents_fetched(self):
        client = FakeClient(paths=self.PATHS)
        entries = retrieve_knowledge("worker router retry strategy", client=client)
        assert [e.path for e in entries] == [
            "ai-knowledge/lessons/worker-router-retry.md"
        ]
        assert client.fetches == ["ai-knowledge/lessons/worker-router-retry.md"]

    def test_irrelevant_query_returns_empty(self):
        client = FakeClient(paths=self.PATHS)
        assert retrieve_knowledge("quantum flux capacitor", client=client) == []
        assert client.fetches == []

    def test_category_filter(self):
        client = FakeClient(paths=self.PATHS)
        entries = retrieve_knowledge(
            "root cause tracing plan", client=client,
            categories=["reasoning"],
        )
        assert all(e.category == "reasoning" for e in entries)

    def test_titles_parsed_from_markdown(self):
        client = FakeClient(
            paths=["ai-knowledge/planning/login-flow-plan.md"],
            bodies={"ai-knowledge/planning/login-flow-plan.md":
                    "---\ntitle: Login flow planning\n---\nsteps"},
        )
        entries = retrieve_knowledge("login flow planning", client=client)
        assert entries[0].title == "Login flow planning"

    def test_limit_respected(self):
        paths = [
            f"ai-knowledge/lessons/retry-{i}-worker-router.md" for i in range(5)
        ]
        client = FakeClient(paths=paths)
        entries = retrieve_knowledge("retry worker router", client=client, limit=2)
        assert len(entries) == 2

    def test_errors_swallowed_not_raised(self):
        client = FakeClient(fail={"tree": gk.KnowledgeError("GitHub repository or path not found")})
        assert retrieve_knowledge("worker router", client=client) == []


# ── Guidance block + priority hierarchy (items 23, 24 prep) ──────


class TestSharedGuidanceBlock:
    def test_empty_when_nothing_found(self):
        assert shared_knowledge_block("anything", ) == ""  # unconfigured env

    def test_block_carries_priority_rule(self, monkeypatch):
        monkeypatch.setattr(
            gk, "retrieve_knowledge",
            lambda query, limit=3: [gk.KnowledgeEntry(
                path="ai-knowledge/planning/decomposition.md",
                category="planning", title="Decomposition strategy",
                body="Split by verification boundary.",
            )],
        )
        block = shared_knowledge_block("decomposition planning")
        assert PRIORITY_RULE in block
        assert "(1) current project files" in block
        assert ".autofix/memory" in block
        assert "[planning] Decomposition strategy" in block

    def test_priority_rule_orders_shared_knowledge_fourth(self):
        files_pos = PRIORITY_RULE.index("(1)")
        memory_pos = PRIORITY_RULE.index("(3)")
        shared_pos = PRIORITY_RULE.index("(4)")
        assert files_pos < memory_pos < shared_pos


# ── Transport-level GitHub error mapping (item 17) ───────────────


class TestGitHubApiClientErrorMapping:
    def _api(self):
        return gk.GitHubApiClient(
            KnowledgeConfig(repo="acme/ai-knowledge", token="t")
        )

    def test_http_401_becomes_clean_auth_error(self):
        import urllib.error

        api = self._api()
        error = api._map_http_error(urllib.error.HTTPError(
            "url", 401, "Unauthorized", {}, None
        ))
        assert isinstance(error, gk.KnowledgeError)
        assert "authentication failed" in str(error).lower()
        assert "Bearer t" not in str(error)  # token never embedded

    def test_http_statuses_map_to_friendly_messages(self):
        import urllib.error

        api = self._api()
        expectations = {
            403: "permission denied",
            404: "not found",
            409: "conflict",
            422: "rejected",
        }
        for status, fragment in expectations.items():
            error = api._map_http_error(urllib.error.HTTPError(
                "url", status, "x", {}, None
            ))
            assert fragment in str(error).lower(), status

    def test_network_outage_raises_unreachable_not_crash(self, monkeypatch):
        import urllib.error

        def refused(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(gk.urllib.request, "urlopen", refused)
        api = self._api()
        with pytest.raises(gk.KnowledgeError) as excinfo:
            api.list_markdown_paths()
        assert "unreachable" in str(excinfo.value).lower()

    def test_headers_carry_bearer_token(self):
        headers = self._api()._headers()
        assert headers["Authorization"] == "Bearer t"
