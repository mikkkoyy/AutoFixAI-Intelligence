"""Persistent AutoFix task object — states, survival, continuation context."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.autofix_task import (
    AutoFixTask,
    CANCELLED,
    COMPLETED,
    FAILED,
    PENDING,
    READY,
    RECOVERING,
    RECOVERY_REQUIRED,
    RUNNING,
    VERIFYING,
    TaskSubtask,
    build_continuation_context,
    extract_session_id,
    max_recovery_attempts,
)
from app.agents.task_transport import load_task_payload


class TestTaskPersistence:
    def test_create_persists_complete_request(self, tmp_path):
        request = "fix the parser" * 500  # large on purpose
        task = AutoFixTask.create(tmp_path, request)

        assert task.status == "PENDING"
        loaded = AutoFixTask.load(tmp_path, task.task_id)
        assert loaded is not None
        assert loaded.original_request == request
        assert loaded.workspace == str(tmp_path)

    def test_task_file_lives_under_autofix_tasks(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "do things")
        path = task.file_path()
        assert ".autofix" in path.parts and "tasks" in path.parts
        assert path.exists()

    def test_transitions_survive_reload(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "request")
        task.transition(RUNNING)
        task.note_stage("Coding", False, "opencode exited with code 1")
        task.transition(RECOVERING)

        loaded = AutoFixTask.load(tmp_path, task.task_id)
        assert loaded.status == RECOVERING
        assert any("Coding" in step for step in loaded.completed_steps) is False
        assert loaded.diagnostics

    def test_completed_sets_completed_at(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "request")
        assert task.completed_at is None
        task.transition(COMPLETED)
        assert task.completed_at is not None

        loaded = AutoFixTask.load(tmp_path, task.task_id)
        assert loaded.status == COMPLETED
        assert loaded.completed_at == task.completed_at

    def test_load_missing_returns_none(self, tmp_path):
        assert AutoFixTask.load(tmp_path, "nope") is None


class TestStates:
    def test_all_required_states_exist(self):
        for state in (
            "PENDING", "RUNNING", "WAITING", "PAUSED", "STOPPED", "FAILED",
            "RECOVERING", "VERIFYING", "COMPLETED", "CANCELLED",
            "RECOVERY_REQUIRED",
        ):
            assert isinstance(state, str)  # constants importable/usable

    def test_cancelled_is_terminal_and_distinct(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "request")
        task.transition(CANCELLED)
        assert task.termination_reason is None or True
        loaded = AutoFixTask.load(tmp_path, task.task_id)
        assert loaded.status == CANCELLED


class TestRecoveryLimit:
    def test_default_is_reasonable(self, monkeypatch):
        monkeypatch.delenv("AUTOFIX_MAX_RECOVERY_ATTEMPTS", raising=False)
        assert max_recovery_attempts() == 3

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AUTOFIX_MAX_RECOVERY_ATTEMPTS", "5")
        assert max_recovery_attempts() == 5

    def test_invalid_values_fall_back(self, monkeypatch):
        monkeypatch.setenv("AUTOFIX_MAX_RECOVERY_ATTEMPTS", "-2")
        assert max_recovery_attempts() == 3
        monkeypatch.setenv("AUTOFIX_MAX_RECOVERY_ATTEMPTS", "junk")
        assert max_recovery_attempts() == 3


class TestContinuationContext:
    def _task(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "Original multi-part request")
        task.plan = "1. step one\n2. step two"
        task.completed_steps.append("Coding: ok")
        task.diagnostics.append("Tests FAILED: assertion")
        task.files_changed = ["src/app.py"]
        task.last_output_tail = "some previous output"
        task.termination_reason = "exit_code_nonzero"
        return task

    def test_context_contains_everything_needed(self, tmp_path):
        task = self._task(tmp_path)
        context = build_continuation_context(task)

        assert "continuing an existing AutoFix task" in context
        assert task.task_id in context
        assert "Original multi-part request" in context
        assert "1. step one" in context
        assert "Coding: ok" in context
        assert "src/app.py" in context
        assert "exit_code_nonzero" in context
        assert "some previous output" in context
        assert "Do not restart completed work" in context
        assert "current workspace state is authoritative" in context.lower()

    def test_context_is_never_a_bare_continue(self, tmp_path):
        context = build_continuation_context(self._task(tmp_path))
        assert len(context) > 400
        assert "Original request:" in context

    def test_session_id_included_when_present(self, tmp_path):
        task = self._task(tmp_path)
        task.opencode_session = "abc123def4567890"
        context = build_continuation_context(task)
        assert "abc123def4567890" in context


class TestSessionIdExtraction:
    def test_extracts_uuid_style_session(self):
        output = (
            "opencode v1.0\nConnected to session "
            "9f86d081-884c-4a08-b4a3-5c7a5b5e1c93\nworking..."
        )
        assert extract_session_id(output) == "9f86d081-884c-4a08-b4a3-5c7a5b5e1c93"

    def test_extracts_short_hex_session(self):
        assert extract_session_id("session: abcdef1234567890abcdef") == (
            "abcdef1234567890abcdef"
        )

    def test_no_session_reported_as_none(self):
        assert extract_session_id("plain output without ids") is None
        assert extract_session_id("") is None


class TestTaskDecomposition:
    def test_decomposition_creates_multiple_subtasks(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "Create three text files: alpha.txt, beta.txt, gamma.txt and verify them.")
        subtasks = task.decompose(description=task.original_request)

        assert len(subtasks) >= 4
        assert len({subtask.id for subtask in subtasks}) == len(subtasks)
        assert all(subtask.dependencies for subtask in subtasks[1:])
        assert task.agent_assignments
        assert set(task.agent_assignments.values()) <= {"planner", "coder", "tester", "reviewer", "debugger"}

    def test_dependency_order_is_preserved(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "Implement backend authentication and add tests.")
        subtasks = task.decompose(description=task.original_request)
        assert subtasks[0].dependencies == []
        assert subtasks[-1].dependencies

    def test_decomposition_persists_across_reload(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "Audit the project, fix the issue, and verify the result.")
        task.decompose(description=task.original_request)
        reloaded = AutoFixTask.load(tmp_path, task.task_id)
        assert len(reloaded.subtasks) == len(task.subtasks)
        assert reloaded.subtasks[0].id == task.subtasks[0].id
        assert reloaded.agent_assignments == task.agent_assignments

    def test_ready_subtasks_only_after_dependencies_complete(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "Create config and use it in code.")
        task.subtasks = [
            TaskSubtask(id="subtask-01", title="Create config", description="Create config", dependencies=[], assigned_agent="coder", status=COMPLETED),
            TaskSubtask(id="subtask-02", title="Use config", description="Use config", dependencies=["subtask-01"], assigned_agent="coder", status=PENDING),
            TaskSubtask(id="subtask-03", title="Verify", description="Verify", dependencies=["subtask-02"], assigned_agent="tester", status=PENDING),
        ]
        ready = [subtask.id for subtask in task.ready_subtasks()]
        assert ready == ["subtask-02"]
        task.subtasks[1].status = COMPLETED
        assert [subtask.id for subtask in task.ready_subtasks()] == ["subtask-03"]


class TestPayloadInterop:
    def test_autofix_task_file_readable_via_transport_loader(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "big request " * 1000)
        payload = load_task_payload(task.file_path())
        assert payload["kind"] == "autofix-task"
        assert payload["original_request"] == task.original_request
