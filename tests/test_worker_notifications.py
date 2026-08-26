"""Worker authentication/configuration notifications.

Covers the safe WorkerNotification model, its emission from the WorkerRouter
(fallback must continue automatically) and the MainWindow display handlers.
Notifications are observability-only: they never influence routing,
verification or completion.
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.autofix_task import AutoFixTask
from app.agents.coding_agent import CodingResult
from app.agents.worker_notifications import (
    EVENT_AUTHENTICATION_REQUIRED,
    EVENT_CONFIGURATION_REQUIRED,
    EVENT_NO_WORKER_AVAILABLE,
    EVENT_UNAVAILABLE,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    WorkerNotification,
    no_worker_available_notification,
    worker_notification,
)
from app.agents.worker_router import WorkerRouter


class FakeWorker:
    """Deterministic internal-worker double."""

    def __init__(self, name, available=True, results=None):
        self.name = name
        self._available = available
        self._results = list(results or [])
        self.calls = []

    def discover(self):
        from app.agents.coding_agent import BackendInfo

        return BackendInfo(
            self.name,
            self._available,
            f"{self.name}-exe" if self._available else None,
            "fake",
        )

    def is_available(self):
        return self._available

    def execute(self, prompt, workspace, on_output=None, timeout=None):
        self.calls.append(prompt)
        if not self._results:
            raise AssertionError(f"{self.name} ran out of scripted results")
        return self._results.pop(0)


def make_router(notifications, opencode, deepseek=None, copilot=None):
    router = WorkerRouter(
        worker_factories={
            "opencode": (lambda: opencode),
            "deepseek": (lambda: deepseek or FakeWorker("deepseek", available=False)),
            "copilot": (lambda: copilot or FakeWorker("copilot", available=False)),
        },
        on_notification=notifications.append,
    )
    return router


def auth_error(worker):
    return CodingResult(
        backend=worker,
        success=False,
        started=True,
        output="",
        error=f"{worker} HTTP 401: authentication/configuration error",
    )


class TestNotificationModel:
    def test_opencode_messages(self):
        assert worker_notification("opencode", "AUTHENTICATION_ERROR").message == (
            "OpenCode authentication required."
        )
        assert worker_notification("opencode", "INVALID_CONFIGURATION").message == (
            "OpenCode configuration required."
        )
        assert worker_notification("opencode", "UNAVAILABLE").message == (
            "OpenCode is unavailable."
        )

    def test_deepseek_messages(self):
        assert worker_notification("deepseek", "AUTHENTICATION_ERROR").message == (
            "DeepSeek API key required."
        )
        assert worker_notification("deepseek", "INVALID_CONFIGURATION").message == (
            "DeepSeek configuration required."
        )
        assert worker_notification("deepseek", "UNAVAILABLE").message == (
            "DeepSeek is unavailable."
        )

    def test_copilot_messages(self):
        assert worker_notification("copilot", "AUTHENTICATION_ERROR").message == (
            "GitHub Copilot authentication required."
        )
        assert worker_notification("copilot", "INVALID_CONFIGURATION").message == (
            "GitHub Copilot configuration required."
        )
        assert worker_notification("copilot", "UNAVAILABLE").message == (
            "GitHub Copilot is unavailable."
        )

    def test_success_and_transient_outcomes_are_silent(self):
        assert worker_notification("opencode", "SUCCESS") is None
        assert worker_notification("opencode", "EXECUTION_ERROR") is None

    def test_timeout_emits_notification(self):
        n = worker_notification("opencode", "TIMEOUT")
        assert n is not None
        assert n.event_type == "worker_timeout"
        assert n.severity == SEVERITY_WARNING

    def test_structured_metadata(self):
        n = worker_notification("deepseek", "AUTHENTICATION_ERROR")
        assert isinstance(n, WorkerNotification)
        data = n.to_dict()
        assert data["worker"] == "deepseek"
        assert data["event_type"] == EVENT_AUTHENTICATION_REQUIRED
        assert data["severity"] == SEVERITY_WARNING
        assert data["can_continue"] is True

    def test_consolidated_failure_lists_worker_status(self):
        n = no_worker_available_notification({
            "opencode": "AUTHENTICATION_ERROR",
            "deepseek": "AUTHENTICATION_ERROR",
            "copilot": "AUTHENTICATION_ERROR",
        })
        assert n.event_type == EVENT_NO_WORKER_AVAILABLE
        assert n.severity == SEVERITY_ERROR
        assert n.can_continue is False
        assert n.message == "AutoFix could not find an available worker."
        assert "- OpenCode — authentication required" in n.detail
        assert "- DeepSeek — API key required" in n.detail
        assert "- Copilot — authentication required" in n.detail


class TestRouterEmitsAuthNotifications:
    def test_opencode_auth_error_notifies_then_falls_back_to_deepseek(self, tmp_path):
        notifications = []
        oc = FakeWorker("opencode", available=True, results=[auth_error("opencode")])
        ds = FakeWorker("deepseek", available=True, results=[
            CodingResult(backend="deepseek", success=True, output="done")
        ])
        router = make_router(notifications, oc, ds)

        result = router.execute("p", str(tmp_path), subtask_id="subtask-01")

        assert result.success is True and result.worker_name == "deepseek"
        assert len(ds.calls) == 1  # DeepSeek was attempted automatically
        events = [n.to_dict() for n in notifications]
        auth = [e for e in events if e["event_type"] == EVENT_AUTHENTICATION_REQUIRED]
        assert len(auth) == 1
        assert auth[0]["worker"] == "opencode"
        assert auth[0]["message"] == "OpenCode authentication required."
        assert auth[0]["severity"] == SEVERITY_WARNING
        assert auth[0]["can_continue"] is True
        # No consolidated failure — a worker executed successfully.
        assert all(e["event_type"] != EVENT_NO_WORKER_AVAILABLE for e in events)

    def test_deepseek_missing_key_notifies_then_falls_back_to_copilot(self, tmp_path):
        notifications = []
        oc = FakeWorker("opencode", available=False)
        ds = FakeWorker("deepseek", available=False)
        co = FakeWorker("copilot", available=True, results=[
            CodingResult(backend="copilot", success=True, output="done")
        ])
        router = make_router(notifications, oc, ds, co)

        result = router.execute("p", str(tmp_path), subtask_id="s1")

        assert result.success is True and result.worker_name == "copilot"
        events = [n.to_dict() for n in notifications]
        by_worker = {e["worker"]: e for e in events}
        # Discovery-time unavailability of both preferred workers.
        assert by_worker["opencode"]["event_type"] == EVENT_UNAVAILABLE
        assert by_worker["opencode"]["severity"] == SEVERITY_INFO
        assert by_worker["deepseek"]["event_type"] == EVENT_UNAVAILABLE
        assert by_worker["deepseek"]["message"] == "DeepSeek is unavailable."
        # Copilot ran; nothing failed overall.
        assert all(e["event_type"] != EVENT_NO_WORKER_AVAILABLE for e in events)

    def test_runtime_auth_error_classification_preserved_in_history(self, tmp_path):
        notifications = []
        task = AutoFixTask.create(tmp_path, "task")
        oc = FakeWorker("opencode", available=True, results=[auth_error("opencode")])
        ds = FakeWorker("deepseek", available=True, results=[auth_error("deepseek")])
        co = FakeWorker("copilot", available=True, results=[auth_error("copilot")])
        router = make_router(notifications, oc, ds, co)

        result = router.execute("p", str(tmp_path), task=task, subtask_id="subtask-01")

        assert result.success is False
        statuses = [(e["worker"], e["status"]) for e in task.worker_history]
        assert statuses == [
            ("opencode", "auth_error"),
            ("deepseek", "auth_error"),
            ("copilot", "auth_error"),
            ("router", "failed"),
        ]
        auth_events = [
            n.to_dict() for n in notifications
            if n.event_type == EVENT_AUTHENTICATION_REQUIRED
        ]
        assert [e["worker"] for e in auth_events] == ["opencode", "deepseek", "copilot"]

    def test_all_workers_unreachable_emits_one_consolidated_event(self, tmp_path):
        notifications = []
        task = AutoFixTask.create(tmp_path, "task")
        router = make_router(
            notifications,
            FakeWorker("opencode", available=True, results=[auth_error("opencode")]),
            FakeWorker("deepseek", available=False),
            FakeWorker("copilot", available=False),
        )

        result = router.execute("p", str(tmp_path), task=task, subtask_id="s1")

        assert result.success is False
        events = [n.to_dict() for n in notifications]
        consolidated = [e for e in events if e["event_type"] == EVENT_NO_WORKER_AVAILABLE]
        assert len(consolidated) == 1
        detail = consolidated[0]["detail"]
        assert "- OpenCode — authentication required" in detail
        assert "- DeepSeek — unavailable" in detail
        assert "- Copilot — unavailable" in detail

    def test_refresh_workers_clears_auth_markdown(self, tmp_path):
        notifications = []
        flaky = FakeWorker("deepseek", available=True, results=[auth_error("deepseek")])
        router = make_router(notifications, FakeWorker("opencode", available=False), flaky)
        router.execute("p", str(tmp_path))
        assert router.discover_workers()["deepseek"].available is False

        # Credentials get fixed externally → re-evaluation succeeds again.
        flaky._results.append(CodingResult(backend="deepseek", success=True, output="ok"))
        router.refresh_workers()
        assert router.select_worker() == "deepseek"


class TestSecurity:
    SECRET = "sk-super-secret-value-9876"

    def test_notifications_never_contain_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", self.SECRET)
        notifications = []
        leaking_error = (
            f"Authorization: Bearer {self.SECRET}\n"
            f'config: {{"api_key": "{self.SECRET}"}}\n'
            f"auth.json contents leaked: {self.SECRET}"
        )
        oc = FakeWorker("opencode", available=True, results=[
            CodingResult(backend="opencode", success=False, started=True, error=leaking_error)
        ])
        ds = FakeWorker("deepseek", available=True, results=[
            CodingResult(backend="deepseek", success=False, started=True, error=leaking_error)
        ])
        co = FakeWorker("copilot", available=True, results=[
            CodingResult(backend="copilot", success=False, started=True, error=leaking_error)
        ])
        router = make_router(notifications, oc, ds, co)

        result = router.execute("p", str(tmp_path))

        assert result.success is False
        blob = json.dumps([n.to_dict() for n in notifications])
        assert self.SECRET not in blob
        assert "Bearer" not in blob
        assert "auth.json" not in blob


class TestMainWindowHandler:
    def test_warning_notification_visible_and_non_blocking(self, window):
        window._on_worker_notification({
            "worker": "opencode",
            "event_type": EVENT_AUTHENTICATION_REQUIRED,
            "severity": SEVERITY_WARNING,
            "message": "OpenCode authentication required.",
            "can_continue": True,
        })
        transcript = "\n".join(
            text for speaker, text in getattr(window, "_conversation_history", [])
        ) or window.conversation.toPlainText()
        assert "OpenCode authentication required." in transcript
        assert "continuing with the next available worker" in transcript

    def test_duplicate_warning_shown_once_per_task(self, window):
        payload = {
            "worker": "opencode",
            "event_type": EVENT_AUTHENTICATION_REQUIRED,
            "severity": SEVERITY_WARNING,
            "message": "OpenCode authentication required.",
            "can_continue": True,
        }
        window._on_worker_notification(payload)
        window._on_worker_notification(payload)
        transcript = window.conversation.toPlainText()
        assert transcript.count("⚠ OpenCode authentication required.") == 1

    def test_all_worker_failure_notification_is_actionable(self, window):
        window._on_worker_notification({
            "worker": "router",
            "event_type": EVENT_NO_WORKER_AVAILABLE,
            "severity": SEVERITY_ERROR,
            "message": "AutoFix could not find an available worker.",
            "can_continue": False,
            "detail": (
                "Worker status:\n"
                "- OpenCode — authentication required\n"
                "- DeepSeek — API key required\n"
                "- Copilot — authentication required"
            ),
        })
        transcript = window.conversation.toPlainText()
        assert "No AutoFix worker is available." in transcript
        assert "- OpenCode — authentication required" in transcript
        assert "- DeepSeek — API key required" in transcript
        assert "- Copilot — authentication required" in transcript
