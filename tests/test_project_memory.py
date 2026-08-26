"""Project memory — storage, retrieval, redaction, and cleanup safety.

Test H — memory kinds stored and relevant records retrieved.
Test I — cleanup can NEVER delete anything outside .autofix\\memory\\.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json

from app.agents.task_memory import (
    KIND_CONVERSATIONS,
    KIND_DECISIONS,
    KIND_ERRORS,
    KIND_FIXES,
    KIND_SESSIONS,
    KIND_TASKS,
    cleanup_memory,
    delete_memory_paths,
    load_task_record,
    memory_dir,
    memory_kind_dir,
    record_memory,
    record_session,
    redact_secrets,
    retrieve_relevant,
)


class TestMemoryStorage:
    def test_all_kinds_stored_in_own_directories(self, tmp_path):
        for kind in (
            KIND_CONVERSATIONS, KIND_TASKS, KIND_SESSIONS,
            KIND_FIXES, KIND_ERRORS, KIND_DECISIONS,
        ):
            path = record_memory(tmp_path, kind, f"{kind} title", f"{kind} content about parser")
            assert path.parent == memory_kind_dir(tmp_path, kind)
            assert path.exists()

        for kind in (
            KIND_CONVERSATIONS, KIND_TASKS, KIND_SESSIONS,
            KIND_FIXES, KIND_ERRORS, KIND_DECISIONS,
        ):
            directory = memory_kind_dir(tmp_path, kind)
            assert directory.is_dir()
            assert list(directory.glob("*.json"))

    def test_task_record_roundtrip(self, tmp_path):
        path = record_memory(
            tmp_path, KIND_TASKS, "task one", "content", tags=["a"]
        )
        record = load_task_record(path)
        assert record["title"] == "task one"
        assert record["tags"] == ["a"]

    def test_session_record_keeps_session_id(self, tmp_path):
        path = record_session(
            tmp_path,
            session_id="abc123def4567890",
            title="opencode-run",
            content="finished",
        )
        record = load_task_record(path)
        assert record["session_id"] == "abc123def4567890"
        assert record["kind"] == KIND_SESSIONS


class TestRedaction:
    def test_api_key_values_redacted(self):
        text = "config: api_key = sk-abcdef1234567890 and password=hunter2"
        red = redact_secrets(text)
        assert "sk-abcdef1234567890" not in red
        assert "hunter2" not in red
        assert "[REDACTED]" in red

    def test_bearer_and_github_tokens_redacted(self):
        red = redact_secrets("Authorization: Bearer abc.def.ghi\nghp_" + "x" * 30)
        assert "abc.def.ghi" not in red
        assert "ghp_" + "x" * 30 not in red

    def test_records_are_redacted_on_disk(self, tmp_path):
        path = record_memory(
            tmp_path, KIND_ERRORS, "boom", "failed with api_key=supersecret123"
        )
        raw = path.read_text(encoding="utf-8")
        assert "supersecret123" not in raw
        assert "[REDACTED]" in raw


class TestRetrieval:
    def _seed(self, tmp_path):
        record_memory(
            tmp_path, KIND_ERRORS, "pytest import error",
            "ModuleNotFoundError: no module named 'parsermod' during AutoFix run",
            tags=["autofix"],
        )
        record_memory(
            tmp_path, KIND_FIXES, "fixed websocket reconnect",
            "changed retry backoff in network layer; verified with tests",
        )
        record_memory(
            tmp_path, KIND_DECISIONS, "use pytest only",
            "project decision: all verification must use pytest",
        )
        record_memory(
            tmp_path, KIND_CONVERSATIONS, "chat about ui colors",
            "user prefers dark theme accents",
        )

    def test_relevant_error_retrieved_first(self, tmp_path):
        self._seed(tmp_path)
        results = retrieve_relevant(tmp_path, "parsermod ModuleNotFoundError")
        assert results
        assert results[0]["kind"] == KIND_ERRORS

    def test_decision_retrieved_for_verification_queries(self, tmp_path):
        self._seed(tmp_path)
        results = retrieve_relevant(tmp_path, "verification pytest requirements")
        kinds = [r["kind"] for r in results]
        assert KIND_DECISIONS in kinds

    def test_unrelated_query_returns_nothing(self, tmp_path):
        self._seed(tmp_path)
        assert retrieve_relevant(tmp_path, "quantum blockchain xylophone") == []

    def test_empty_query_returns_nothing(self, tmp_path):
        self._seed(tmp_path)
        assert retrieve_relevant(tmp_path, "") == []

    def test_never_returns_everything(self, tmp_path):
        self._seed(tmp_path)
        results = retrieve_relevant(tmp_path, "parsermod", limit=5)
        assert len(results) < 4  # seeded 4 records; only relevant ones returned


class TestCleanupSafety:
    def test_cleanup_only_touches_memory_directory(self, tmp_path):
        # Project source files that must survive ANY cleanup.
        (tmp_path / "src").mkdir()
        source = tmp_path / "src" / "main.py"
        source.write_text("print('keep me')\n", encoding="utf-8")
        config = tmp_path / "pyproject.json"
        config.write_text("{}", encoding="utf-8")

        record_memory(tmp_path, KIND_ERRORS, "old error", "old content")
        record_memory(tmp_path, KIND_FIXES, "old fix", "fix content")

        removed = cleanup_memory(tmp_path)  # full sweep of memory dir

        assert removed >= 2
        assert source.exists() and source.read_text(encoding="utf-8") == "print('keep me')\n"
        assert config.exists()

    def test_cleanup_respects_age_cutoff(self, tmp_path):
        path = record_memory(tmp_path, KIND_ERRORS, "recent", "content")
        assert cleanup_memory(tmp_path, older_than_days=7) == 0
        assert path.exists()
        assert cleanup_memory(tmp_path, older_than_days=0) >= 1
        assert not path.exists()

    def test_delete_memory_paths_refuses_project_files(self, tmp_path):
        source = tmp_path / "app.py"
        source.write_text("KEEP = True\n", encoding="utf-8")
        memory_file = record_memory(tmp_path, KIND_FIXES, "f", "c")

        removed = delete_memory_paths(tmp_path, [source, memory_file])

        assert removed == 1
        assert source.exists()          # project file untouched
        assert not memory_file.exists()  # memory file deleted

    def test_delete_memory_paths_refuses_outside_workspace(self, tmp_path):
        outside = tmp_path.parent / "outside-target.txt"
        outside.write_text("data", encoding="utf-8")
        try:
            assert delete_memory_paths(tmp_path, [outside]) == 0
            assert outside.exists()
        finally:
            outside.unlink(missing_ok=True)

    def test_cleanup_on_missing_directory_is_noop(self, tmp_path):
        assert cleanup_memory(tmp_path / "nonexistent") == 0

    def test_memory_root_is_inside_autofix(self, tmp_path):
        root = memory_dir(tmp_path)
        assert str(root).endswith(".autofix\\memory") or str(root).endswith(".autofix/memory")
