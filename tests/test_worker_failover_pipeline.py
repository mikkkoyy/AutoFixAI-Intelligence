"""End-to-end AutoFix failover through ApprovalPipeline + WorkerRouter.

These tests exercise the REAL pipeline code path (default CodingAgentRunner →
WorkerRouter) with deterministic injected worker doubles, so no real OpenCode
install is touched.

Covers:
    — fallback when the preferred worker is unavailable (same subtask)
    — all workers unavailable → honest failure, no fake completion
    — same-subtask fallback with persisted worker history
    — recovery of the SAME AutoFixTask after availability changes
    — multi-subtask regression over the router path
    — verification failure does NOT switch workers
"""

import re
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.autofix_task import AutoFixTask, COMPLETED, FAILED, RECOVERY_REQUIRED
from app.agents.coding_agent import BackendInfo, CodingResult
from app.agents.pipeline import ApprovalPipeline
from app.agents.worker_notifications import (
    EVENT_AUTHENTICATION_REQUIRED,
    EVENT_NO_WORKER_AVAILABLE,
)
from app.agents.worker_router import (
    NO_AVAILABLE_WORKER_MESSAGE,
    WorkerRouter,
)


class FakeWorker:
    """Deterministic internal-worker double."""

    def __init__(self, name, available=True, script=None):
        self.name = name
        self._available = available
        self._script = list(script or [])
        self.calls = []

    def discover(self):
        return BackendInfo(
            self.name,
            self._available,
            f"{self.name}-exe" if self._available else None,
            "fake",
        )

    def is_available(self):
        return self._available

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        from pathlib import Path

        self.calls.append({"prompt": prompt, "workspace": str(workspace)})
        if not self._script:
            raise AssertionError(f"{self.name} ran out of scripted results")
        item = self._script.pop(0)
        if callable(item):
            return item(prompt, Path(workspace))
        return item


def writing_worker(name, fail_after=None):
    """A worker that creates the .txt file named in the SUBTASK TITLE.

    Verification-style subtasks create nothing; they check that every .txt
    file referenced by the prompt exists (like a real coding agent would).
    """

    def make():
        state = {"executions": 0}

        def run(prompt, root):
            state["executions"] += 1
            if fail_after is not None and state["executions"] > fail_after:
                return CodingResult(
                    backend=name,
                    success=False,
                    output="partial work",
                    error=f"{name} exited with code 1",
                )
            title_match = re.search(r"Subtask title:\s*(.+)", prompt)
            title = title_match.group(1) if title_match else ""
            target_names = []
            for match in re.findall(r"[A-Za-z0-9_.-]+\.txt", title):
                if match not in target_names:
                    target_names.append(match)
            for fname in target_names:
                stem = fname.split(".", 1)[0]
                (root / fname).write_text(stem, encoding="utf-8")
            if "verify" in title.lower() or "validation" in title.lower() or not target_names:
                referenced = []
                for match in re.findall(r"[A-Za-z0-9_.-]+\.txt", prompt):
                    if match not in referenced:
                        referenced.append(match)
                missing = [n for n in referenced if not (root / n).exists()]
                if missing:
                    return CodingResult(
                        backend=name, success=False,
                        error=f"verification failed, missing {missing}",
                    )
            return CodingResult(backend=name, success=True, output="subtask executed")

        worker = FakeWorker(name, available=True, script=[run] * 12)
        return worker

    return make


def unavailable(name):
    return (lambda: FakeWorker(name, available=False))


def router_with(opencode, deepseek, copilot):
    return WorkerRouter(worker_factories={
        "opencode": opencode,
        "deepseek": deepseek,
        "copilot": copilot,
    })


def run_pipeline(pipeline):
    finished = []
    statuses = []
    pipeline.status_changed.connect(statuses.append)
    pipeline.pipeline_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    pipeline.run()
    return statuses, finished


THREE_FILES = (
    "Create three files: alpha.txt, beta.txt, gamma.txt. "
    "Put the exact filename inside each file. Then verify all three files."
)


class TestFallbackRuntime:
    def test_opencode_unavailable_deepseek_executes_same_task(self, tmp_path):
        request = (
            "Create fallback_test.txt containing:\n\n"
            "AutoFix fallback verification\n\nThen verify the file."
        )

        def deepseek_run(prompt, root):
            for fname in dict.fromkeys(re.findall(r"[A-Za-z0-9_.-]+\.txt", prompt)):
                root.write_text("AutoFix fallback verification", encoding="utf-8") \
                    if False else (root / fname).write_text(
                        "AutoFix fallback verification", encoding="utf-8")
            return CodingResult(backend="deepseek", success=True, output="created")

        deepseek = FakeWorker("deepseek", available=True, script=[deepseek_run] * 6)
        router = router_with(unavailable("opencode"), lambda: deepseek, unavailable("copilot"))

        pipeline = ApprovalPipeline(request, str(tmp_path), worker_router=router)
        statuses, finished = run_pipeline(pipeline)

        task = pipeline.autofix_task
        assert task is not None
        assert pipeline.final_state == COMPLETED
        assert finished[0][0] is True
        assert task.verified is True

        # The file was created by the fallback worker.
        assert (tmp_path / "fallback_test.txt").exists()
        assert "AutoFix fallback verification" in (tmp_path / "fallback_test.txt").read_text(encoding="utf-8")

        # Exactly ONE parent task on disk.
        tasks_dir = tmp_path / ".autofix" / "tasks"
        task_files = [p for p in tasks_dir.iterdir() if p.name.startswith("autofix-task-")]
        assert len(task_files) == 1

        # Worker history: OpenCode UNAVAILABLE, DeepSeek EXECUTED — same subtasks.
        seq = [(e["worker"], e["status"]) for e in task.worker_history]
        assert ("opencode", "unavailable") in seq
        assert ("deepseek", "completed") in seq
        completed_subtasks = {
            e["subtask"] for e in task.worker_history
            if e["worker"] == "deepseek" and e["status"] == "completed"
        }
        assert completed_subtasks == {s.id for s in task.subtasks}

        reloaded = AutoFixTask.load(tmp_path, task.task_id)
        assert reloaded.status == COMPLETED
        assert reloaded.task_id == task.task_id
        assert "AutoFix: Completed" in "\n".join(statuses)

    def test_all_workers_unavailable_is_a_real_failure(self, tmp_path):
        router = router_with(
            unavailable("opencode"), unavailable("deepseek"), unavailable("copilot")
        )
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        statuses, finished = run_pipeline(pipeline)

        task = pipeline.autofix_task
        assert finished[0][0] is False
        assert NO_AVAILABLE_WORKER_MESSAGE in finished[0][1]
        assert pipeline.final_state == FAILED
        assert task.status != COMPLETED
        assert task.verified is not True

        reloaded = AutoFixTask.load(tmp_path, task.task_id)
        assert reloaded.status == FAILED
        assert NO_AVAILABLE_WORKER_MESSAGE in (reloaded.last_error or "")
        # No file was fabricated.
        assert not (tmp_path / "alpha.txt").exists()
        # Every worker attempt is recorded as unavailable.
        statuses_seen = {(e["worker"], e["status"]) for e in reloaded.worker_history}
        assert ("opencode", "unavailable") in statuses_seen
        assert ("deepseek", "unavailable") in statuses_seen
        assert ("copilot", "unavailable") in statuses_seen


class TestSameSubtaskFallback:
    def test_opencode_fails_deepseek_completes_one_subtask_one_task(self, tmp_path):
        opencode = FakeWorker(
            "opencode", available=True,
            script=[CodingResult(
                backend="opencode", success=False, started=True,
                output="crashed before modifying anything",
                error="opencode exited with code 1",
            )] * 12,
        )
        router = router_with(lambda: opencode, writing_worker("deepseek"), unavailable("copilot"))

        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        _statuses, finished = run_pipeline(pipeline)

        task = pipeline.autofix_task
        assert finished[0][0] is True
        assert pipeline.final_state == COMPLETED

        # Exactly ONE parent AutoFixTask and ONE subtask-01.
        tasks_dir = tmp_path / ".autofix" / "tasks"
        task_files = [p for p in tasks_dir.iterdir() if p.name.startswith("autofix-task-")]
        assert len(task_files) == 1
        ids = [s.id for s in task.subtasks]
        assert len(ids) == len(set(ids))
        assert ids.count("subtask-01") == 1

        # Worker history shows the same-subtask sequence.
        s1 = [(e["worker"], e["status"]) for e in task.worker_history if e["subtask"] == "subtask-01"]
        assert s1 == [("opencode", "failed"), ("deepseek", "completed")]

        assert all((tmp_path / n).exists() for n in ("alpha.txt", "beta.txt", "gamma.txt"))
        assert task.verified is True

    def test_verification_failure_does_not_switch_workers(self, tmp_path):
        # OpenCode "succeeds" but writes nothing useful → the SUBTASK
        # verification fails. That is an implementation problem: the router
        # must NOT be asked to try another worker for that subtask.
        opencode = FakeWorker(
            "opencode", available=True,
            script=[CodingResult(backend="opencode", success=True, output="did nothing")] * 12,
        )

        def deepseek_run(prompt, root):  # would have fixed it — never called
            for fname in dict.fromkeys(re.findall(r"[A-Za-z0-9_.-]+\.txt", prompt)):
                (root / fname).write_text(fname.split(".", 1)[0], encoding="utf-8")
            return CodingResult(backend="deepseek", success=True, output="done")

        deepseek = FakeWorker("deepseek", available=True, script=[deepseek_run] * 12)
        router = router_with(lambda: opencode, lambda: deepseek, unavailable("copilot"))

        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        _statuses, finished = run_pipeline(pipeline)

        assert finished[0][0] is False
        assert pipeline.final_state in (FAILED, RECOVERY_REQUIRED)
        assert deepseek.calls == []  # no worker switch on verification failure
        # The failed subtask stays honestly incomplete.
        task = pipeline.autofix_task
        assert any(s.status != COMPLETED for s in task.subtasks)


class TestRecoverySameTask:
    def test_recovery_resumes_same_task_without_repeating_completed_work(self, tmp_path):
        # Phase 1: deepseek completes subtask-01 then fails; nothing else
        # available → the task fails honestly after subtask-01 completed.
        flaky_deepseek = FakeWorker("deepseek", available=True)

        def phase1_run(prompt, root):
            from pathlib import Path
            root = Path(root)
            names = []
            for match in re.findall(r"[A-Za-z0-9_.-]+\.txt", prompt):
                if match not in names:
                    names.append(match)
            if "subtask-02" in prompt:
                return CodingResult(
                    backend="deepseek", success=False, started=True,
                    error="DeepSeek HTTP 503: API error",
                )
            for fname in names:
                (root / fname).write_text(fname.split(".", 1)[0], encoding="utf-8")
            return CodingResult(backend="deepseek", success=True, output="created")

        flaky_deepseek._script = [phase1_run] * 12
        router1 = router_with(unavailable("opencode"), lambda: flaky_deepseek, unavailable("copilot"))

        pipeline1 = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router1)
        _s1, finished1 = run_pipeline(pipeline1)
        task1 = pipeline1.autofix_task

        assert finished1[0][0] is False
        assert task1.status == FAILED
        first = task1.subtask_by_id("subtask-01")
        second = task1.subtask_by_id("subtask-02")
        assert first.status == COMPLETED
        assert second.status == FAILED
        alpha_mtime = (tmp_path / "alpha.txt").stat().st_mtime

        # Phase 2: availability changed (DeepSeek healthy again) — recover the
        # SAME task, never a new one.
        loaded = AutoFixTask.load(tmp_path, task1.task_id)
        router2 = router_with(unavailable("opencode"), writing_worker("deepseek"), unavailable("copilot"))
        deepseek2_calls = []

        class RecordingRouter(WorkerRouter):
            def execute(self, prompt, workspace, **kwargs):
                result = super().execute(prompt, workspace, **kwargs)
                deepseek2_calls.append((kwargs.get("subtask_id"), result.success))
                return result

        router2 = RecordingRouter(worker_factories={
            "opencode": unavailable("opencode"),
            "deepseek": writing_worker("deepseek"),
            "copilot": unavailable("copilot"),
        })

        pipeline2 = ApprovalPipeline(
            THREE_FILES, str(tmp_path), existing_task=loaded, worker_router=router2,
        )
        _s2, finished2 = run_pipeline(pipeline2)

        task2 = pipeline2.autofix_task
        assert task2.task_id == task1.task_id  # SAME AutoFixTask

        assert finished2[0][0] is True
        assert pipeline2.final_state == COMPLETED
        assert task2.verified is True

        # subtask-01 remains COMPLETED and was NOT re-executed.
        still_first = task2.subtask_by_id("subtask-01")
        assert still_first.status == COMPLETED
        assert still_first.completed_at == first.completed_at
        assert all(sid != "subtask-01" for sid, _ok in deepseek2_calls)

        # No duplicate subtasks were created.
        ids = [s.id for s in task2.subtasks]
        assert len(ids) == len(set(ids))

        # Completed work was not redone on disk either.
        assert (tmp_path / "alpha.txt").stat().st_mtime == alpha_mtime

        reloaded = AutoFixTask.load(tmp_path, task1.task_id)
        assert reloaded.status == COMPLETED
        assert all(s.status == COMPLETED for s in reloaded.subtasks)


class TestMultiSubtaskRegression:
    def test_four_subtasks_complete_over_router_path(self, tmp_path):
        router = router_with(writing_worker("opencode"), writing_worker("deepseek"), unavailable("copilot"))
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        _statuses, finished = run_pipeline(pipeline)

        task = pipeline.autofix_task
        assert finished[0][0] is True
        assert len(task.subtasks) == 4
        assert all(s.status == COMPLETED for s in task.subtasks)
        assert all(s.verified for s in task.subtasks)
        assert task.verified is True
        assert task.status == COMPLETED
        # Normal execution: only OpenCode ran, no fallback needed.
        assert {e["worker"] for e in task.worker_history} == {"opencode"}


class TestWorkerNotificationSignal:
    """The pipeline re-emits safe worker notifications as a Qt signal (D)."""

    def test_auth_warning_signal_emitted_and_fallback_continues(self, tmp_path):
        opencode = FakeWorker(
            "opencode", available=True,
            script=[CodingResult(
                backend="opencode", success=False, started=True,
                error="OpenCode HTTP 401: authentication/configuration error",
            )] * 12,
        )
        received = []
        router = WorkerRouter(
            worker_factories={
                "opencode": lambda: opencode,
                "deepseek": writing_worker("deepseek"),
                "copilot": unavailable("copilot"),
            },
            on_notification=lambda n: received.append(n.to_dict()),
        )
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        pipeline.worker_notification.connect(received.append)
        _statuses, finished = run_pipeline(pipeline)

        # Fallback continued automatically despite the auth problem.
        assert finished[0][0] is True
        assert pipeline.final_state == COMPLETED

        events = [dict(e) for e in received]
        warnings = [
            e for e in events if e["event_type"] == EVENT_AUTHENTICATION_REQUIRED
        ]
        assert len(warnings) >= 1  # one per subtask attempt on OpenCode
        assert all(
            e["message"] == "OpenCode authentication required."
            and e["severity"] == "warning"
            and e["can_continue"] is True
            for e in warnings
        )
        # No failure event — the task completed.
        assert all(e["event_type"] != EVENT_NO_WORKER_AVAILABLE for e in events)

    def test_consolidated_failure_signal_when_no_worker_available(self, tmp_path):
        received = []
        router = WorkerRouter(
            worker_factories={
                "opencode": unavailable("opencode"),
                "deepseek": unavailable("deepseek"),
                "copilot": unavailable("copilot"),
            },
            on_notification=lambda n: received.append(n.to_dict()),
        )
        pipeline = ApprovalPipeline(THREE_FILES, str(tmp_path), worker_router=router)
        pipeline.worker_notification.connect(received.append)
        _statuses, finished = run_pipeline(pipeline)

        assert finished[0][0] is False
        events = [dict(e) for e in received]
        consolidated = [
            e for e in events if e["event_type"] == EVENT_NO_WORKER_AVAILABLE
        ]
        assert len(consolidated) >= 1
        detail = consolidated[0]["detail"]
        assert "- OpenCode — unavailable" in detail
        assert "- DeepSeek — unavailable" in detail
        assert "- Copilot — unavailable" in detail
