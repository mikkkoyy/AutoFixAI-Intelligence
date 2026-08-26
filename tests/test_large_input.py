"""Automatic large-input detection, task memory and recovery tests.

Large/multi-line requests flow through the normal AutoFix path. The complete
original request must be preserved and never truncated.
"""

import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.large_input import (
    DEFAULT_LARGE_INPUT_THRESHOLD,
    is_large_input,
    large_input_threshold,
    summarize_large_input,
)
from app.agents.task_memory import (
    describe_remaining,
    load_incomplete_tasks,
    load_task_record,
    memory_dir,
    save_task_record,
    update_task_record,
)


# ── Threshold configuration ────────────────────────────────────


class TestThreshold:
    def test_default_threshold(self):
        assert large_input_threshold(env={}) == DEFAULT_LARGE_INPUT_THRESHOLD

    def test_env_override(self):
        assert large_input_threshold(env={"AUTOFIX_LARGE_INPUT_THRESHOLD": "50"}) == 50

    def test_invalid_env_falls_back_to_default(self):
        assert large_input_threshold(env={"AUTOFIX_LARGE_INPUT_THRESHOLD": "abc"}) == (
            DEFAULT_LARGE_INPUT_THRESHOLD
        )

    def test_non_positive_env_falls_back_to_default(self):
        assert large_input_threshold(env={"AUTOFIX_LARGE_INPUT_THRESHOLD": "0"}) == (
            DEFAULT_LARGE_INPUT_THRESHOLD
        )
        assert large_input_threshold(env={"AUTOFIX_LARGE_INPUT_THRESHOLD": "-5"}) == (
            DEFAULT_LARGE_INPUT_THRESHOLD
        )

    def test_boundary_detection(self):
        assert not is_large_input("x" * 49, env={"AUTOFIX_LARGE_INPUT_THRESHOLD": "50"})
        assert is_large_input("x" * 50, env={"AUTOFIX_LARGE_INPUT_THRESHOLD": "50"})
        assert not is_large_input("", env={})
        assert not is_large_input(None, env={})


# ── Compact summary (never embeds the full text) ───────────────


class TestSummary:
    def test_summary_contains_stats_and_preview(self):
        text = "\n".join(f"line-{i}" for i in range(500))
        summary = summarize_large_input(text)

        assert "500 lines" in summary
        assert "line-0" in summary
        assert "line-11" in summary
        assert "line-499" not in summary
        assert "complete text preserved" in summary

    def test_summary_is_compact_for_huge_input(self):
        text = "x" * 200_000
        assert len(summarize_large_input(text)) < 2_000


# ── Task memory persistence ────────────────────────────────────


class TestTaskMemory:
    def test_save_creates_record_under_workspace_memory(self, tmp_path):
        path = save_task_record(tmp_path, "fix the parser", status="received")

        expected_dir = tmp_path / ".autofix" / "memory"
        assert path.parent == expected_dir
        assert path.exists()
        assert path.name.startswith("task-")
        assert path.suffix == ".json"

    def test_full_request_preserved_untruncated(self, tmp_path):
        huge = "\n".join(f"def fn_{i}(): return {i}" for i in range(2000))
        assert len(huge) > 50_000

        path = save_task_record(tmp_path, huge)
        record = load_task_record(path)

        assert record["request"] == huge
        assert record["stats"]["characters"] == len(huge)
        assert record["stats"]["lines"] == len(huge.splitlines())

    def test_update_merges_fields_and_appends(self, tmp_path):
        path = save_task_record(tmp_path, "task body")

        update_task_record(path, status="planned", plan="1. do it")
        update_task_record(
            path, append_stage={"stage": "Coding", "ok": False, "message": "boom"}
        )
        update_task_record(path, append_error="OpenCode stopped")

        record = load_task_record(path)
        assert record["status"] == "planned"
        assert record["plan"] == "1. do it"
        assert record["stages"] == [
            {"stage": "Coding", "ok": False, "message": "boom"}
        ]
        assert record["errors"] == ["OpenCode stopped"]
        assert record["updated_at"] >= record["created_at"]

    def test_load_incomplete_filters_completed(self, tmp_path):
        done = save_task_record(tmp_path, "done task")
        open_1 = save_task_record(tmp_path, "stuck task one")
        open_2 = save_task_record(tmp_path, "stuck task two")

        update_task_record(done, status="completed")

        pending = load_incomplete_tasks(tmp_path)
        ids = {r["id"] for r in pending}

        assert len(pending) == 2
        assert load_task_record(done)["id"] not in ids
        assert load_task_record(open_1)["id"] in ids
        assert load_task_record(open_2)["id"] in ids
        assert all("_path" in r for r in pending)

    def test_describe_remaining_mentions_failures(self, tmp_path):
        path = save_task_record(tmp_path, "recover me", status="failed")
        update_task_record(
            path,
            append_stage={"stage": "Coding", "ok": False, "message": "stopped"},
            verified=False,
        )

        record = load_task_record(path)
        description = describe_remaining(record)

        assert "failed" in description
        assert "Coding" in description
        assert "verification" in description

    def test_record_is_valid_json_readable_by_ai(self, tmp_path):
        path = save_task_record(tmp_path, "what was the request?")
        data = json.loads(path.read_text(encoding="utf-8"))

        assert data["kind"] == "autofix-large-task"
        assert data["request"] == "what was the request?"
        for key in ("status", "stages", "errors", "backend", "verified", "remaining"):
            assert key in data


# ── AutoFix integration ────────────────────────────────────────


class TestAutoFixLargeInputIntegration:
    LARGE_ENV = {"AUTOFIX_LARGE_INPUT_THRESHOLD": "400"}

    def _large_text(self, lines=60):
        return "\n".join(f"def helper_{i}(a, b): return a + b + {i}" for i in range(lines))

    def test_small_input_creates_no_memory_record(self, window, tmp_path):
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")
        window.on_chat_send("small fix request")

        assert window._active_task_memory is None
        assert not memory_dir(tmp_path).exists() or (
            list(memory_dir(tmp_path).glob("task-*.json")) == []
        )
        assert "Analyzing the workspace and preparing a plan…" in (
            window.conversation.toPlainText()
        )

    def test_large_input_detected_and_recorded(self, window, tmp_path, monkeypatch):
        monkeypatch.setattr("app.ui.main_window.is_large_input", lambda t, env=None: True)
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")

        large = self._large_text()
        window.on_chat_send(large)

        assert window._active_task_memory is not None
        assert window._active_task_memory.parent == memory_dir(tmp_path)

        record = load_task_record(window._active_task_memory)
        assert record["request"] == large
        assert record["status"] == "received"

        conversation = window.conversation.toPlainText()
        assert "Processing large input" in conversation
        assert "Analyzing the workspace and preparing a plan…" not in conversation

    def test_plan_ready_keeps_full_request_and_updates_memory(
        self, window, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("app.ui.main_window.is_large_input", lambda t, env=None: True)
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")

        large = self._large_text()
        window.on_chat_send(large)
        window._on_plan_ready("1. Do the work")

        assert window._pending_plan["request"] == large

        record = load_task_record(window._active_task_memory)
        assert record["status"] == "planned"
        assert record["plan"] == "1. Do the work"

        conversation = window.conversation.toPlainText()
        assert "complete input is preserved" in conversation

    def test_approval_marks_executing(self, window, tmp_path, monkeypatch):
        from app.agents.pipeline import ApprovalPipeline

        monkeypatch.setattr("app.ui.main_window.is_large_input", lambda t, env=None: True)
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: None)
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")

        window.on_chat_send(self._large_text())
        window._on_plan_ready("plan")
        window.on_approve_plan()

        record = load_task_record(window._active_task_memory)
        assert record["status"] == "executing"

    def test_stage_results_logged_to_memory(self, window, tmp_path, monkeypatch):
        monkeypatch.setattr("app.ui.main_window.is_large_input", lambda t, env=None: True)
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")

        window.on_chat_send(self._large_text())
        window._on_stage_finished("Coding", False, "opencode exited with code 1\nmore")

        record = load_task_record(window._active_task_memory)
        assert record["stages"] == [
            {"stage": "Coding", "ok": False, "message": "opencode exited with code 1"}
        ]

    def test_failed_pipeline_saves_recovery_state(self, window, tmp_path, monkeypatch):
        monkeypatch.setattr("app.ui.main_window.is_large_input", lambda t, env=None: True)
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")

        large = self._large_text()
        window.on_chat_send(large)
        memory_path = window._active_task_memory
        window._on_stage_finished("Coding", False, "OpenCode stopped unexpectedly")
        window._on_pipeline_finished(False, "Execution FAILED — no code changes were verified.")

        assert window._active_task_memory is None  # cleared after finalization

        records = load_incomplete_tasks(tmp_path)
        assert len(records) == 1
        saved = records[0]
        assert saved["status"] == "failed"
        assert saved["request"] == large
        assert saved["verified"] is False
        assert saved["errors"]
        assert saved["remaining"]

        conversation = window.conversation.toPlainText()
        assert "saved for recovery" in conversation

    def test_successful_pipeline_completes_record(self, window, tmp_path, monkeypatch):
        monkeypatch.setattr("app.ui.main_window.is_large_input", lambda t, env=None: True)
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")

        window.on_chat_send(self._large_text())
        memory_path = window._active_task_memory
        window._on_pipeline_finished(True, "Execution PASSED — coding, tests and verification succeeded.")

        assert load_incomplete_tasks(tmp_path) == []
        saved = load_task_record(memory_path)
        assert saved["status"] == "completed"
        assert saved["verified"] is True

    def test_normal_autofix_flow_untouched_by_memory(self, window, tmp_path):
        window.set_active_workspace(str(tmp_path))
        window.set_ai_mode("autofix")

        window.on_chat_send("tiny task")
        window._last_request = "tiny task"
        window._on_plan_ready("1. small plan")

        # No memory machinery involved for small tasks.
        assert window._active_task_memory is None
        assert window.approve_button.isVisibleTo(window)
        assert window._pending_plan["request"] == "tiny task"
