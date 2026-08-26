"""WorkerRouter failover semantics.

Covers the worker result model, same-subtask fallback, worker history
persistence and the honest all-workers-unavailable failure.
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.autofix_task import AutoFixTask
from app.agents.coding_agent import BackendInfo, CodingResult
from app.agents.worker_router import (
    DEFAULT_PRIORITY,
    NO_AVAILABLE_WORKER_MESSAGE,
    WORKER_AUTHENTICATION_ERROR,
    WORKER_EXECUTION_ERROR,
    WORKER_INVALID_CONFIGURATION,
    WORKER_QUOTA_EXCEEDED,
    WORKER_SUCCESS,
    WORKER_TIMEOUT,
    WORKER_UNAVAILABLE,
    WorkerRouter,
    classify_worker_result,
)


class FakeWorker:
    """Deterministic internal-worker double."""

    def __init__(self, name="fake", available=True, results=None):
        self.name = name
        self._available = available
        self._results = list(results or [])
        self.calls = []

    def discover(self):
        return BackendInfo(
            self.name,
            self._available,
            f"{self.name}-exe" if self._available else None,
            "fake worker",
        )

    def is_available(self):
        return self._available

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt, "workspace": str(workspace), "timeout": timeout})
        if not self._results:
            raise AssertionError(f"{self.name} ran out of scripted results")
        item = self._results.pop(0)
        if callable(item):
            return item(prompt, workspace)
        return item


def ok(worker="opencode", output="done"):
    return CodingResult(backend=worker, success=True, output=output)


def make_router(opencode, deepseek=None, copilot=None):
    return WorkerRouter(
        worker_factories={
            "opencode": (lambda: opencode),
            "deepseek": (lambda: deepseek or FakeWorker("deepseek", available=False)),
            "copilot": (lambda: copilot or FakeWorker("copilot", available=False)),
        }
    )


class TestClassification:
    def test_success(self):
        assert classify_worker_result(ok()) == WORKER_SUCCESS

    def test_timeout(self):
        result = CodingResult(backend="opencode", success=False, timed_out=True)
        assert classify_worker_result(result) == WORKER_TIMEOUT

    def test_not_started_is_unavailable(self):
        result = CodingResult(backend="deepseek", success=False, started=False)
        assert classify_worker_result(result) == WORKER_UNAVAILABLE

    def test_authentication_error(self):
        result = CodingResult(
            backend="deepseek", success=False,
            error="DeepSeek HTTP 401: authentication/configuration error",
        )
        assert classify_worker_result(result) == WORKER_AUTHENTICATION_ERROR

    def test_invalid_configuration(self):
        result = CodingResult(
            backend="opencode", success=False,
            error="opencode rejected the configured model. Unknown model 'x'.",
        )
        assert classify_worker_result(result) == WORKER_INVALID_CONFIGURATION

    def test_quota_exceeded_rate_limit(self):
        result = CodingResult(
            backend="deepseek", success=False,
            error="DeepSeek HTTP 429: rate limit exceeded",
        )
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_quota_exceeded_quota(self):
        result = CodingResult(
            backend="deepseek", success=False,
            error="Insufficient quota for this request",
        )
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_quota_exceeded_billing(self):
        result = CodingResult(
            backend="copilot", success=False,
            error="credits exhausted, billing required",
        )
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_plain_crash_is_execution_error(self):
        result = CodingResult(
            backend="opencode", success=False, error="opencode exited with code 1"
        )
        assert classify_worker_result(result) == WORKER_EXECUTION_ERROR


class TestFallbackChain:
    def test_opencode_unavailable_falls_back_to_deepseek_same_subtask(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "create a file")
        oc = FakeWorker("opencode", available=False)
        ds = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(oc, ds)

        result = router.execute("prompt", str(tmp_path), task=task, subtask_id="subtask-01")

        assert result.success is True
        assert result.worker_name == "deepseek"
        # Same subtask, auditable sequence: unavailable first, executed second.
        assert [(e["subtask"], e["worker"], e["status"]) for e in task.worker_history] == [
            ("subtask-01", "opencode", "unavailable"),
            ("subtask-01", "deepseek", "completed"),
        ]
        assert len(ds.calls) == 1  # the SAME subtask was executed once

    def test_execution_failure_falls_back_to_next_worker(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=True, results=[
            CodingResult(backend="opencode", success=False, error="opencode exited with code 1")
        ])
        ds = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(oc, ds)

        result = router.execute("prompt", str(tmp_path), task=task, subtask_id="subtask-02")

        assert result.success is True
        assert [e["worker"] for e in task.worker_history if e["subtask"] == "subtask-02"] == [
            "opencode", "deepseek",
        ]
        statuses = [e["status"] for e in task.worker_history]
        assert statuses == ["failed", "completed"]

    def test_timeout_recorded_then_fallback(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=True, results=[
            CodingResult(
                backend="opencode", success=False, timed_out=True,
                error="opencode exceeded 900s timeout.",
            )
        ])
        ds = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(oc, ds)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="subtask-01")

        assert result.success is True
        timeout_entry = next(e for e in task.worker_history if e["status"] == "timeout")
        assert timeout_entry["worker"] == "opencode"
        assert timeout_entry["subtask"] == "subtask-01"
        assert "timeout" in timeout_entry["reason"].lower()

    def test_auth_failure_marks_worker_down_and_continues(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=False)
        ds = FakeWorker("deepseek", available=True, results=[
            CodingResult(
                backend="deepseek", success=False, started=True,
                error="DeepSeek HTTP 401: authentication/configuration error",
            )
        ])
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(oc, ds, co)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is True
        assert result.worker_name == "copilot"
        statuses = [(e["worker"], e["status"]) for e in task.worker_history]
        assert statuses == [
            ("opencode", "unavailable"),
            ("deepseek", "auth_error"),
            ("copilot", "completed"),
        ]
        # The misconfigured worker is skipped for later selections until refresh.
        record = router.discover_workers()["deepseek"]
        assert record.available is False
        assert router.select_worker() == "copilot"

    def test_deepseek_unavailable_falls_back_to_copilot(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=False)
        ds = FakeWorker("deepseek", available=False)
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(oc, ds, co)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is True
        assert result.worker_name == "copilot"
        assert [(e["worker"], e["status"]) for e in task.worker_history] == [
            ("opencode", "unavailable"),
            ("deepseek", "unavailable"),
            ("copilot", "completed"),
        ]

    def test_configuration_required_history_status(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=False)
        ds = FakeWorker("deepseek", available=True, results=[
            CodingResult(
                backend="deepseek", success=False, started=True,
                error="DeepSeek rejected the configured model: unknown model",
            )
        ])
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(oc, ds, co)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is True
        entry = next(e for e in task.worker_history if e["worker"] == "deepseek")
        assert entry["status"] == "config_required"


class TestAllWorkersUnavailable:
    def test_honest_failure_no_fake_success(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        router = make_router(
            FakeWorker("opencode", available=False),
            FakeWorker("deepseek", available=False),
            FakeWorker("copilot", available=False),
        )

        result = router.execute("p", str(tmp_path), task=task, subtask_id="subtask-09")

        assert result.success is False
        assert result.backend is None
        assert NO_AVAILABLE_WORKER_MESSAGE in result.error
        statuses = [(e["worker"], e["status"]) for e in task.worker_history]
        assert statuses == [
            ("opencode", "unavailable"),
            ("deepseek", "unavailable"),
            ("copilot", "unavailable"),
            ("router", "failed"),
        ]

    def test_every_worker_failing_still_fails_honestly(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        crash = CodingResult(backend="x", success=False, error="{name} crashed")
        router = make_router(
            FakeWorker("opencode", available=True, results=[crash]),
            FakeWorker("deepseek", available=True, results=[crash]),
            FakeWorker("copilot", available=True, results=[crash]),
        )

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is False
        assert NO_AVAILABLE_WORKER_MESSAGE in result.error
        assert all(e["status"] == "failed" for e in task.worker_history[:-1])


class TestHistoryPersistence:
    def test_worker_history_persisted_to_disk_with_subtask_ids(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=False)
        ds = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(oc, ds)

        router.execute("p", str(tmp_path), task=task, subtask_id="subtask-03")

        reloaded = AutoFixTask.load(tmp_path, task.task_id)
        assert reloaded.worker_history == [
            {
                "subtask": "subtask-03",
                "worker": "opencode",
                "status": "unavailable",
                **{k: v for k, v in reloaded.worker_history[0].items()
                   if k not in {"subtask", "worker", "status"}},
            },
            {
                "subtask": "subtask-03",
                "worker": "deepseek",
                "status": "completed",
                **{k: v for k, v in reloaded.worker_history[1].items()
                   if k not in {"subtask", "worker", "status"}},
            },
        ]
        raw = json.loads((tmp_path / ".autofix" / "tasks" / f"{task.task_id}.json").read_text(encoding="utf-8"))
        assert raw["worker_history"][0]["subtask"] == "subtask-03"

    def test_default_priority_is_repository_configured_chain(self):
        assert DEFAULT_PRIORITY == ("opencode", "deepseek", "copilot")


class TestRefreshAndSelection:
    def test_refresh_reevaluates_availability(self, tmp_path):
        flaky = FakeWorker("deepseek", available=False)
        router = make_router(FakeWorker("opencode", available=False), flaky)
        assert router.select_worker() is None

        flaky._available = True
        assert router.select_worker() is None  # cached — still stale
        router.refresh_workers()
        assert router.select_worker() == "deepseek"

    def test_preferred_worker_hoisted_over_priority(self, tmp_path):
        router = make_router(
            FakeWorker("opencode", available=True, results=[ok()]),
            FakeWorker("deepseek", available=True, results=[ok("deepseek")]),
        )
        result = router.execute("p", str(tmp_path))
        assert result.backend == "opencode"

    def test_unknown_worker_name_rejected(self, tmp_path):
        router = WorkerRouter(priority=("bogus",))
        result = router.execute("p", str(tmp_path))
        assert result.success is False
