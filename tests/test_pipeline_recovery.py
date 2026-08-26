"""Pipeline recovery semantics — PROCESS EXIT != TASK COMPLETION.

Covers:
    B — OpenCode completes            → COMPLETED
    C — OpenCode stops early          → recovery, SAME task continues
    D — OpenCode crashes              → state saved, recovery
    E — manual cancellation           → CANCELLED, no auto-restart
    F — recovery limit                → RECOVERY_REQUIRED
    G — original context preservation through failure→recovery→completion
    J — UI: Chat|AutoFix|OpenCode remain, Bulk gone, APPROVE & EXECUTE kept
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.autofix_task import (
    AutoFixTask,
    CANCELLED,
    COMPLETED,
    RECOVERY_REQUIRED,
)
from app.agents.coding_agent import CodingResult
from app.agents.pipeline import ApprovalPipeline
from app.agents.task_transport import load_task_payload


class ScriptedRunner:
    """CodingAgentRunner double with a scripted sequence of results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.cancel_requested = False

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt, "workspace": str(workspace)})
        if not self._results:
            raise AssertionError(
                f"ScriptedRunner ran out of results "
                f"(unexpected execute call #{len(self.calls)})"
            )
        result = self._results.pop(0)
        if callable(result):
            result = result(self)
        return result

    def cancel_active(self):
        self.cancel_requested = True


def ok(backend="opencode", output="done"):
    return CodingResult(backend=backend, success=True, output=output)


def stopped(output="partial work", error="opencode exited with code 1"):
    return CodingResult(
        backend="opencode", success=False, output=output, error=error
    )


def make_workspace_with_passing_tests(tmp_path):
    (tmp_path / "mathlib.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "test_mathlib.py").write_text(
        "from mathlib import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return tmp_path


def run_pipeline(pipeline):
    stages = []
    statuses = []
    finished = []
    pipeline.stage_finished.connect(lambda label, ok_, msg: stages.append((label, ok_)))
    pipeline.status_changed.connect(statuses.append)
    pipeline.pipeline_finished.connect(
        lambda success, summary: finished.append((success, summary))
    )
    pipeline.run()
    return stages, statuses, finished


class TestBNormalCompletion:
    def test_success_means_completed_not_just_exit_zero(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        runner = ScriptedRunner([ok()])

        pipeline = ApprovalPipeline("add feature", str(workspace), coding_runner=runner)
        _stages, _statuses, finished = run_pipeline(pipeline)

        assert len(finished) == 1
        success, summary = finished[0]
        assert success is True
        assert "PASSED" in summary

        task = pipeline.autofix_task
        assert task.status == COMPLETED
        assert task.verified is True
        assert task.recovery_attempts == 0
        assert task.completed_at is not None
        # Persisted on disk too.
        reloaded = AutoFixTask.load(workspace, task.task_id)
        assert reloaded.status == COMPLETED

    def test_exit_zero_without_verification_is_not_completion(self, tmp_path):
        # Coding agent exits zero but the tests fail → NOT completed.
        (tmp_path / "test_broken.py").write_text(
            "def test_always_fails():\n    assert False\n", encoding="utf-8"
        )
        runner = ScriptedRunner([ok()] * 8)  # every attempt "succeeds"
        pipeline = ApprovalPipeline("do work", str(tmp_path), coding_runner=runner)
        _stages, _statuses, finished = run_pipeline(pipeline)

        success, _summary = finished[0]
        assert success is False
        assert pipeline.autofix_task.status != COMPLETED
        assert pipeline.final_state in (RECOVERY_REQUIRED, "FAILED")


class TestCStopsEarlyThenRecovers:
    def test_same_task_continues_and_completes(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        runner = ScriptedRunner([stopped(), ok()])

        pipeline = ApprovalPipeline("implement feature X", str(workspace), coding_runner=runner)
        _stages, statuses, finished = run_pipeline(pipeline)

        success, _summary = finished[0]
        assert success is True
        assert pipeline.final_state == COMPLETED
        assert pipeline.autofix_task.recovery_attempts == 1
        assert len(runner.calls) == 2

        # The continuation is the SAME logical task, not a new one.
        continuation = runner.calls[1]["prompt"]
        assert "continuing an existing autofix task" in continuation.lower()
        assert "implement feature X" in continuation
        assert "Do not restart completed work" in continuation

        # Status surfaced to the UI.
        joined = "\n".join(statuses)
        assert "AutoFix: Recovering" in joined
        assert "OpenCode: Continuing" in joined
        assert "AutoFix: Completed" in joined

    def test_state_persisted_between_stop_and_recovery(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        runner = ScriptedRunner([stopped(), ok()])
        pipeline = ApprovalPipeline("task", str(workspace), coding_runner=runner)
        run_pipeline(pipeline)

        reloaded = AutoFixTask.load(workspace, pipeline.autofix_task.task_id)
        assert reloaded.original_request == "task"
        assert reloaded.recovery_attempts == 1
        assert reloaded.termination_reason == "completed"


class TestDCrashRecovery:
    def test_crash_saves_state_then_recovers(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        crash = CodingResult(
            backend="opencode",
            success=False,
            output="session 9f86d081-884c-4a08-b4a3-5c7a5b5e1c93\ntraceback...",
            error="process crashed",
        )
        runner = ScriptedRunner([crash, ok()])

        pipeline = ApprovalPipeline("fix crash", str(workspace), coding_runner=runner)
        _stages, _statuses, finished = run_pipeline(pipeline)

        success, _summary = finished[0]
        assert success is True
        task = pipeline.autofix_task
        assert task.status == COMPLETED
        # Session identifier preserved when available.
        assert task.opencode_session == "9f86d081-884c-4a08-b4a3-5c7a5b5e1c93"
        # Crash output captured on the persistent record (latest tail is the
        # successful retry; earlier attempts are kept in output_history).
        assert "traceback" in "\n".join(task.output_history or [])


class TestEManualCancellation:
    def test_cancel_stops_without_restart(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)

        def cancel_during_run(runner_self):
            runner_self._pipeline_ref.cancel()
            return stopped(error="terminated by user")

        runner = ScriptedRunner([cancel_during_run])
        pipeline = ApprovalPipeline("long task", str(workspace), coding_runner=runner)
        runner._pipeline_ref = pipeline
        _stages, statuses, finished = run_pipeline(pipeline)

        assert len(runner.calls) == 1  # NO automatic restart
        success, summary = finished[0]
        assert success is False
        assert "Cancelled" in summary
        assert pipeline.final_state == CANCELLED
        assert pipeline.autofix_task.status == CANCELLED

        reloaded = AutoFixTask.load(workspace, pipeline.autofix_task.task_id)
        assert reloaded.status == CANCELLED
        assert "AutoFix: Cancelled" in "\n".join(statuses)


class TestFRecoveryLimit:
    def test_limit_reached_reports_recovery_required(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.agents.pipeline.max_recovery_attempts", lambda: 2)
        workspace = make_workspace_with_passing_tests(tmp_path)
        runner = ScriptedRunner([stopped()] * 3)  # always fails

        pipeline = ApprovalPipeline("impossible task", str(workspace), coding_runner=runner)
        _stages, statuses, finished = run_pipeline(pipeline)

        # Initial attempt + exactly 2 recoveries.
        assert len(runner.calls) == 3
        success, summary = finished[0]
        assert success is False
        assert pipeline.final_state == RECOVERY_REQUIRED
        assert pipeline.autofix_task.status == RECOVERY_REQUIRED

        assert "Completed:" in summary
        assert "Remaining:" in summary
        assert "Last error:" in summary
        assert "Recovery attempts: 2" in summary
        assert "AutoFix: Recovery Required" in "\n".join(statuses)

        reloaded = AutoFixTask.load(workspace, pipeline.autofix_task.task_id)
        assert reloaded.status == RECOVERY_REQUIRED
        assert reloaded.last_error


class TestGOriginalContextPreserved:
    def test_large_request_survives_failure_recovery_completion(self, tmp_path):
        workspace = make_workspace_with_passing_tests(tmp_path)
        big_request = (
            "# Large specification\n"
            + "\n".join(
                f"Step {i}: update module_{i} to satisfy requirement R-{i}. "
                "Keep the public API stable and add regression tests."
                for i in range(600)
            )
        )
        assert len(big_request) > 30000

        runner = ScriptedRunner([stopped(), ok()])
        pipeline = ApprovalPipeline(big_request, str(workspace), coding_runner=runner)
        _stages, _statuses, finished = run_pipeline(pipeline)

        success, _summary = finished[0]
        assert success is True

        task = pipeline.autofix_task
        assert task.original_request == big_request  # never truncated

        reloaded = AutoFixTask.load(workspace, task.task_id)
        assert reloaded.original_request == big_request

        # Continuation prompt carried the complete original request.
        continuation = runner.calls[1]["prompt"]
        assert big_request in continuation

        # And the transport payload still holds it byte-for-byte.
        payload = load_task_payload(task.file_path())
        assert payload["original_request"] == big_request


class TestJUiModes:
    def test_modes_bulk_present_approve_always_visible(self, window):
        labels = [b.text() for b in window._mode_buttons.values()]
        assert labels == ["Chat", "AutoFix"]

        # APPROVE & EXECUTE never disappears — it is disabled without a plan.
        assert window.approve_button.text() == "APPROVE & EXECUTE"
        assert window.approve_button.isVisibleTo(window) is True
        assert window.approve_button.isEnabled() is False

        assert hasattr(window, "stop_button")
        assert window.stop_button.isVisibleTo(window) is False
