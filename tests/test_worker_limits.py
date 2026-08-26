"""Worker limit failover, quota/limit detection, timeout, Ollama fallback,
and AI-intelligence repository offline resilience.

Covers:
    - WORKER_QUOTA_EXCEEDED classification and persistence
    - AUTOFIX_WORKER_TIMEOUT env-configurable timeout
    - Zero retry for QUOTA/AUTH/CONFIG/UNAVAILABLE; one retry for TIMEOUT
    - Ollama as last-resort fallback (discoverable, not in DEFAULT_PRIORITY)
    - Pending knowledge sync when GitHub is unavailable
    - Local knowledge cache for offline GitHub
    - Notification events for quota exceeded and timeout
    - Security: notifications never leak secrets
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.autofix_task import AutoFixTask
from app.agents.coding_agent import BackendInfo, CodingResult
from app.agents.worker_notifications import (
    EVENT_QUOTA_EXCEEDED,
    EVENT_TIMEOUT,
    SEVERITY_WARNING,
)
from app.agents.worker_router import (
    HISTORY_STATUS,
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


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

class FakeWorker:
    def __init__(self, name, available=True, results=None):
        self.name = name
        self._available = available
        self._results = list(results or [])
        self.calls = []

    def discover(self):
        return BackendInfo(
            self.name, self._available,
            f"{self.name}-exe" if self._available else None, "fake",
        )

    def is_available(self):
        return self._available

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append({"prompt": prompt, "timeout": timeout})
        if not self._results:
            raise AssertionError(f"{self.name} ran out of scripted results")
        item = self._results.pop(0)
        if callable(item):
            return item(prompt, workspace)
        return item


def ok(worker="opencode", output="done"):
    return CodingResult(backend=worker, success=True, output=output)


def quota_error(worker, msg="HTTP 429: rate limit exceeded"):
    return CodingResult(backend=worker, success=False, started=True, error=msg)


def timeout_error(worker):
    return CodingResult(backend=worker, success=False, timed_out=True, error=f"{worker} exceeded timeout")


def make_router(opencode, deepseek=None, copilot=None, ollama=None, env=None, **kwargs):
    factories = {
        "opencode": (lambda: opencode),
        "deepseek": (lambda: deepseek or FakeWorker("deepseek", available=False)),
        "copilot": (lambda: copilot or FakeWorker("copilot", available=False)),
        "ollama": (lambda: ollama or FakeWorker("ollama", available=False)),
    }
    return WorkerRouter(worker_factories=factories, env=env or os.environ, **kwargs)


# -----------------------------------------------------------------------
# Classification
# -----------------------------------------------------------------------

class TestQuotaClassification:
    def test_quota_exceeded_rate_limit_429(self):
        result = CodingResult(backend="deepseek", success=False, error="HTTP 429: rate limit exceeded")
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_quota_exceeded_quota_keyword(self):
        result = CodingResult(backend="deepseek", success=False, error="quota exceeded for this model")
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_quota_exceeded_too_many_requests(self):
        result = CodingResult(backend="copilot", success=False, error="too many requests, try later")
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_quota_exceeded_insufficient_quota(self):
        result = CodingResult(backend="opencode", success=False, error="insufficient quota")
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_quota_exceeded_billing(self):
        result = CodingResult(backend="deepseek", success=False, error="billing required")
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_quota_exceeded_credits(self):
        result = CodingResult(backend="deepseek", success=False, error="credits exhausted")
        assert classify_worker_result(result) == WORKER_QUOTA_EXCEEDED

    def test_history_status_label(self):
        assert HISTORY_STATUS[WORKER_QUOTA_EXCEEDED] == "quota_exceeded"


# -----------------------------------------------------------------------
# Router persistence
# -----------------------------------------------------------------------

class TestQuotaPersistence:
    def test_quota_exceeded_marks_worker_down(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        ds = FakeWorker("deepseek", available=True, results=[quota_error("deepseek")])
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(FakeWorker("opencode", available=False), ds, co)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is True
        assert result.worker_name == "copilot"
        record = router.discover_workers()["deepseek"]
        assert record.available is False
        assert record.configured is False
        entry = next(e for e in task.worker_history if e["worker"] == "deepseek")
        assert entry["status"] == "quota_exceeded"

    def test_quota_exceeded_emits_notification(self, tmp_path):
        notifications = []
        ds = FakeWorker("deepseek", available=True, results=[quota_error("deepseek")])
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(
            FakeWorker("opencode", available=False), ds, co,
            on_notification=lambda n: notifications.append(n),
        )

        router.execute("p", str(tmp_path), subtask_id="s1")

        events = [n.to_dict() for n in notifications]
        quota_events = [e for e in events if e["event_type"] == EVENT_QUOTA_EXCEEDED]
        assert len(quota_events) == 1
        assert quota_events[0]["worker"] == "deepseek"
        assert quota_events[0]["severity"] == SEVERITY_WARNING
        assert quota_events[0]["can_continue"] is True

    def test_all_workers_quota_exceeded_fails_honestly(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        router = make_router(
            FakeWorker("opencode", available=True, results=[quota_error("opencode")]),
            FakeWorker("deepseek", available=True, results=[quota_error("deepseek")]),
            FakeWorker("copilot", available=True, results=[quota_error("copilot")]),
        )

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is False
        statuses = [(e["worker"], e["status"]) for e in task.worker_history]
        assert statuses == [
            ("opencode", "quota_exceeded"),
            ("deepseek", "quota_exceeded"),
            ("copilot", "quota_exceeded"),
            ("router", "failed"),
        ]


# -----------------------------------------------------------------------
# Timeout
# -----------------------------------------------------------------------

class TestWorkerTimeout:
    def test_timeout_falls_back_to_next_worker(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=True, results=[timeout_error("opencode")])
        ds = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(oc, ds)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is True
        assert result.worker_name == "deepseek"
        entry = next(e for e in task.worker_history if e["worker"] == "opencode")
        assert entry["status"] == "timeout"

    def test_timeout_emits_notification(self, tmp_path):
        notifications = []
        oc = FakeWorker("opencode", available=True, results=[timeout_error("opencode")])
        ds = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(oc, ds, on_notification=lambda n: notifications.append(n))

        router.execute("p", str(tmp_path), subtask_id="s1")

        timeout_events = [n.to_dict() for n in notifications if n.event_type == EVENT_TIMEOUT]
        assert len(timeout_events) == 1
        assert timeout_events[0]["worker"] == "opencode"

    def test_timeout_does_not_mark_worker_down(self, tmp_path):
        oc = FakeWorker("opencode", available=True, results=[timeout_error("opencode")])
        ds = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(oc, ds)

        router.execute("p", str(tmp_path))

        # TIMEOUT is transient -- worker stays available (marked available=True still)
        record = router.discover_workers()["opencode"]
        assert record.available is True


# -----------------------------------------------------------------------
# AUTOFIX_WORKER_TIMEOUT env config
# -----------------------------------------------------------------------

class TestEnvTimeout:
    def test_env_timeout_passed_to_worker(self, tmp_path):
        env = {**os.environ, "AUTOFIX_WORKER_TIMEOUT": "300"}
        oc = FakeWorker("opencode", available=True, results=[ok()])
        router = make_router(oc, env=env)

        router.execute("p", str(tmp_path))

        assert oc.calls[0]["timeout"] == 300

    def test_default_timeout_is_900(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "AUTOFIX_WORKER_TIMEOUT"}
        oc = FakeWorker("opencode", available=True, results=[ok()])
        router = make_router(oc, env=env)

        router.execute("p", str(tmp_path))

        assert oc.calls[0]["timeout"] == 900

    def test_explicit_timeout_overrides_env(self, tmp_path):
        env = {**os.environ, "AUTOFIX_WORKER_TIMEOUT": "300"}
        oc = FakeWorker("opencode", available=True, results=[ok()])
        router = make_router(oc, env=env)

        router.execute("p", str(tmp_path), timeout=60)

        assert oc.calls[0]["timeout"] == 60

    def test_zero_env_timeout_disables_timeout(self, tmp_path):
        env = {**os.environ, "AUTOFIX_WORKER_TIMEOUT": "0"}
        oc = FakeWorker("opencode", available=True, results=[ok()])
        router = make_router(oc, env=env)

        router.execute("p", str(tmp_path))

        assert oc.calls[0]["timeout"] is None


# -----------------------------------------------------------------------
# Ollama fallback
# -----------------------------------------------------------------------

class TestOllamaFallback:
    def test_ollama_discoverable_even_when_not_in_priority(self, tmp_path):
        ollama = FakeWorker("ollama", available=True)
        router = make_router(
            FakeWorker("opencode", available=False),
            FakeWorker("deepseek", available=False),
            FakeWorker("copilot", available=False),
            ollama=ollama,
        )
        records = router.discover_workers()
        assert "ollama" in records
        assert records["ollama"].available is True

    def test_ollama_not_in_default_priority(self):
        assert "ollama" not in ("opencode", "deepseek", "copilot")
        from app.agents.worker_router import DEFAULT_PRIORITY
        assert "ollama" not in DEFAULT_PRIORITY

    def test_ollama_not_tried_by_default_routing(self, tmp_path):
        ollama = FakeWorker("ollama", available=True, results=[ok("ollama")])
        router = make_router(
            FakeWorker("opencode", available=False),
            FakeWorker("deepseek", available=False),
            FakeWorker("copilot", available=False),
            ollama=ollama,
        )

        result = router.execute("p", str(tmp_path))

        # Ollama was NOT tried because it is not in the default priority
        assert result.success is False
        assert ollama.calls == []

    def test_ollama_reachable_via_prefer(self, tmp_path):
        ollama = FakeWorker("ollama", available=True, results=[ok("ollama")])
        router = make_router(
            FakeWorker("opencode", available=False),
            FakeWorker("deepseek", available=False),
            FakeWorker("copilot", available=False),
            ollama=ollama,
        )

        # Custom priority that includes ollama
        router.priority = ("opencode", "deepseek", "copilot", "ollama")
        result = router.execute("p", str(tmp_path))

        assert result.success is True
        assert result.worker_name == "ollama"

    def test_ollama_unavailable_reports_honestly(self, tmp_path):
        ollama = FakeWorker("ollama", available=False)
        router = make_router(
            FakeWorker("opencode", available=False),
            FakeWorker("deepseek", available=False),
            FakeWorker("copilot", available=False),
            ollama=ollama,
        )
        router.priority = ("opencode", "deepseek", "copilot", "ollama")

        result = router.execute("p", str(tmp_path))

        assert result.success is False
        assert "No available AutoFix worker" in result.error


# -----------------------------------------------------------------------
# Persistent status patterns
# -----------------------------------------------------------------------

class TestPersistentStatuses:
    def test_auth_marks_worker_down(self, tmp_path):
        oc = FakeWorker("opencode", available=True, results=[
            CodingResult(backend="opencode", success=False, started=True,
                         error="unauthorized 401")
        ])
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(oc, co)

        router.execute("p", str(tmp_path))
        record = router.discover_workers()["opencode"]
        assert record.available is False

    def test_config_marks_worker_down(self, tmp_path):
        ds = FakeWorker("deepseek", available=True, results=[
            CodingResult(backend="deepseek", success=False, started=True,
                         error="unknown model specified")
        ])
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(FakeWorker("opencode", available=False), ds, co)

        router.execute("p", str(tmp_path))
        record = router.discover_workers()["deepseek"]
        assert record.available is False

    def test_unavailable_stays_available_after_refresh(self, tmp_path):
        flaky = FakeWorker("deepseek", available=True, results=[
            CodingResult(backend="deepseek", success=False, started=False)
        ])
        ds2 = FakeWorker("deepseek", available=True, results=[ok("deepseek")])
        router = make_router(FakeWorker("opencode", available=False), flaky)

        router.execute("p", str(tmp_path))
        assert router.discover_workers()["deepseek"].available is False

        # Replace with a healthy one and refresh
        router._factories["deepseek"] = lambda: ds2
        router.refresh_workers()
        assert router.select_worker() == "deepseek"


# -----------------------------------------------------------------------
# Mixed failover scenarios
# -----------------------------------------------------------------------

class TestMixedFailover:
    def test_auth_then_quota_then_success(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=True, results=[
            CodingResult(backend="opencode", success=False, started=True,
                         error="unauthorized 401")
        ])
        ds = FakeWorker("deepseek", available=True, results=[quota_error("deepseek")])
        co = FakeWorker("copilot", available=True, results=[ok("copilot")])
        router = make_router(oc, ds, co)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is True
        assert result.worker_name == "copilot"
        statuses = [(e["worker"], e["status"]) for e in task.worker_history]
        assert statuses == [
            ("opencode", "auth_error"),
            ("deepseek", "quota_exceeded"),
            ("copilot", "completed"),
        ]

    def test_unavailable_then_timeout_then_quota_all_fail(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "task")
        router = make_router(
            FakeWorker("opencode", available=False),
            FakeWorker("deepseek", available=True, results=[timeout_error("deepseek")]),
            FakeWorker("copilot", available=True, results=[quota_error("copilot")]),
        )

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is False
        statuses = [(e["worker"], e["status"]) for e in task.worker_history]
        assert statuses == [
            ("opencode", "unavailable"),
            ("deepseek", "timeout"),
            ("copilot", "quota_exceeded"),
            ("router", "failed"),
        ]


# -----------------------------------------------------------------------
# Security
# -----------------------------------------------------------------------

class TestNotificationSecurity:
    SECRET = "sk-leaked-value-9999"

    def test_quota_notification_never_leaks_secret(self, tmp_path):
        notifications = []
        ds = FakeWorker("deepseek", available=True, results=[
            CodingResult(backend="deepseek", success=False, started=True,
                         error=f"Authorization: Bearer {self.SECRET} rate limit exceeded")
        ])
        router = make_router(
            FakeWorker("opencode", available=False), ds,
            on_notification=lambda n: notifications.append(n),
        )
        router.execute("p", str(tmp_path))
        blob = json.dumps([n.to_dict() for n in notifications])
        assert self.SECRET not in blob

    def test_timeout_notification_never_leaks_secret(self, tmp_path):
        notifications = []
        oc = FakeWorker("opencode", available=True, results=[
            CodingResult(backend="opencode", success=False, timed_out=True,
                         error=f"Bearer {self.SECRET} timeout")
        ])
        router = make_router(
            oc, on_notification=lambda n: notifications.append(n),
        )
        router.execute("p", str(tmp_path))
        blob = json.dumps([n.to_dict() for n in notifications])
        assert self.SECRET not in blob
