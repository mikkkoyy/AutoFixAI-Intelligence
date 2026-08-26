from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.agents.autofix_task import AutoFixTask
from app.agents.coding_agent import BackendInfo, CodingAgentRunner, CodingResult
from app.agents.worker_notifications import (
    no_worker_available_notification,
    worker_notification,
    WorkerNotification,
)
from app.agents.workers import CopilotWorker, DeepSeekWorker, OpenCodeWorker, OllamaWorker

DEFAULT_PRIORITY = ("opencode", "deepseek", "copilot")

# ---------------------------------------------------------------------------
# Canonical worker outcome statuses.
#
# These distinguish WORKER-level problems (unavailable, misconfigured, timed
# out, crashed) from SUBTASK implementation/verification failures.  Only the
# worker-level problems trigger fallback; verification failure never does.
# ---------------------------------------------------------------------------
WORKER_SUCCESS = "SUCCESS"
WORKER_UNAVAILABLE = "UNAVAILABLE"
WORKER_AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
WORKER_TIMEOUT = "TIMEOUT"
WORKER_EXECUTION_ERROR = "EXECUTION_ERROR"
WORKER_INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
WORKER_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"

#: Persisted (lowercase) history labels — matches the established record style.
HISTORY_STATUS = {
    WORKER_SUCCESS: "completed",
    WORKER_UNAVAILABLE: "unavailable",
    WORKER_AUTHENTICATION_ERROR: "auth_error",
    WORKER_TIMEOUT: "timeout",
    WORKER_EXECUTION_ERROR: "failed",
    WORKER_INVALID_CONFIGURATION: "config_required",
    WORKER_QUOTA_EXCEEDED: "quota_exceeded",
}

#: Honest diagnostic emitted ONLY when every internal worker is unusable.
NO_AVAILABLE_WORKER_MESSAGE = (
    "No available AutoFix worker could execute this subtask."
)

_AUTH_TOKENS = (
    "api key", "unauthorized", "401", "403", "forbidden",
    "authentication", "credentials",
)
_CONFIG_TOKENS = (
    "unknown model", "invalid model", "no such model",
    "configuration required", "not configured", "missing configuration",
)
_EXCEEDED_TOKENS = (
    "rate limit", "quota", "limit exceeded", "too many requests",
    "429", "exceeds", "insufficient quota", "billing",
    "usage limit", "credits exhausted", "payment required",
)

#: Seconds a worker process may run before being killed.  Configurable via
#: AUTOFIX_WORKER_TIMEOUT (environment variable).  Zero means no timeout.
_DEFAULT_WORKER_TIMEOUT = 900


def classify_worker_result(result: CodingResult) -> str:
    """Map a raw :class:`CodingResult` onto the canonical worker statuses."""
    if result.success:
        return WORKER_SUCCESS
    if getattr(result, "timed_out", False):
        return WORKER_TIMEOUT
    if not getattr(result, "started", True):
        # The worker could not even be launched.
        return WORKER_UNAVAILABLE
    text = f"{result.error or ''}".lower()
    if any(token in text for token in _EXCEEDED_TOKENS):
        return WORKER_QUOTA_EXCEEDED
    if any(token in text for token in _CONFIG_TOKENS):
        return WORKER_INVALID_CONFIGURATION
    if any(token in text for token in _AUTH_TOKENS):
        return WORKER_AUTHENTICATION_ERROR
    return WORKER_EXECUTION_ERROR


@dataclass
class WorkerRecord:
    name: str
    available: bool = False
    configured: bool = False
    healthy: bool = False
    detail: str = ""
    attempts: int = 0
    failures: int = 0
    last_status: str = "unknown"


@dataclass
class WorkerOutcome:
    worker: str
    status: str
    reason: str = ""
    result: CodingResult | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _default_worker_factories() -> dict[str, Callable[[], object]]:
    """Real internal workers. Tests may inject deterministic doubles instead."""
    return {
        "opencode": OpenCodeWorker,
        "deepseek": DeepSeekWorker,
        "copilot": CopilotWorker,
        "ollama": OllamaWorker,
    }


class WorkerRouter:
    """Internal failover router for AutoFix coding workers.

    The router is intentionally internal and does not create separate user-facing
    modes. It keeps AutoFix as the single entry point while allowing fallback
    between OpenCode, DeepSeek and Copilot when the preferred worker is not
    usable.

    Failover rules:
      * UNAVAILABLE / AUTHENTICATION_ERROR / INVALID_CONFIGURATION / TIMEOUT /
        EXECUTION_ERROR all try the next worker in the configured priority.
      * A subtask whose verification fails is an IMPLEMENTATION problem — it is
        handled by the existing recovery/verification flow and NEVER by
        switching workers here.
      * Every attempt is persisted on the task's ``worker_history`` with the
        SAME subtask id — fallback reuses the subtask, it never duplicates it.
    """

    def __init__(
        self,
        priority: tuple[str, ...] | None = None,
        env=None,
        worker_factories: dict[str, Callable[[], object]] | None = None,
        on_notification: Callable[[WorkerNotification], None] | None = None,
    ):
        self.env = os.environ if env is None else env
        self.priority = tuple(priority or DEFAULT_PRIORITY)
        self._factories = dict(worker_factories) if worker_factories else _default_worker_factories()
        self._worker_cache: dict[str, WorkerRecord] | None = None
        #: Optional observer for safe, structured worker notifications
        #: (authentication/configuration problems). Purely observational — it
        #: can never influence routing, fallback, verification or completion.
        self.on_notification = on_notification

    def _emit_notification(self, notification: WorkerNotification | None) -> None:
        if notification is None or self.on_notification is None:
            return
        try:
            self.on_notification(notification)
        except Exception:  # observers must never break routing
            pass

    # ------------------------------------------------------------------

    def refresh_workers(self) -> None:
        """Drop cached discovery so availability is re-evaluated.

        Used on same-task recovery after worker availability may have changed.
        """
        self._worker_cache = None

    def _create_worker(self, name: str):
        factory = self._factories.get(name)
        return factory() if factory else None

    def discover_workers(self) -> dict[str, WorkerRecord]:
        if self._worker_cache is not None:
            return self._worker_cache

        records: dict[str, WorkerRecord] = {}
        for name in self.priority:
            if name == "opencode":
                opencode = self._create_worker(name) or OpenCodeWorker()
                info = opencode.discover() if hasattr(opencode, "discover") else BackendInfo("opencode", False, None, "not discoverable")
                ok = bool(info.available and info.executable)
                records[name] = WorkerRecord(
                    name=name,
                    available=ok,
                    configured=True,
                    healthy=ok,
                    detail=info.detail or "OpenCode discovered",
                )
                continue

            worker = self._create_worker(name)
            if worker is None or not hasattr(worker, "is_available"):
                records[name] = WorkerRecord(
                    name=name,
                    available=False,
                    configured=False,
                    healthy=False,
                    detail=f"Unknown worker: {name}",
                )
                continue

            ok = bool(worker.is_available())
            if name == "deepseek":
                detail = "DeepSeek key configured" if ok else (
                    "DeepSeek configuration required: DEEPSEEK_API_KEY missing"
                )
            elif name == "copilot":
                detail = "Copilot CLI discovered" if ok else "Copilot CLI not found"
            elif name == "ollama":
                detail = "Ollama local model ready" if ok else "Ollama not running or model not found"
            else:
                detail = "worker available" if ok else "worker unavailable"
            records[name] = WorkerRecord(
                name=name,
                available=ok,
                configured=ok,
                healthy=ok,
                detail=detail,
            )

        # Discover Ollama as available even when not in priority — it is the
        # last-resort local fallback, discoverable but not tried by default.
        if "ollama" not in records:
            ollama = self._create_worker("ollama")
            if ollama is not None and hasattr(ollama, "is_available"):
                ok = bool(ollama.is_available())
                records["ollama"] = WorkerRecord(
                    name="ollama",
                    available=ok,
                    configured=ok,
                    healthy=ok,
                    detail="Ollama local model ready" if ok else "Ollama not running or model not found",
                )

        self._worker_cache = records
        return records

    def available_workers(self) -> list[str]:
        workers = []
        for name in self.priority:
            record = self.discover_workers().get(name)
            if record and record.available:
                workers.append(name)
        return workers

    def select_worker(self, preferred: str | None = None) -> str | None:
        names = list((preferred,) if preferred else self.priority)
        for name in names + [n for n in self.priority if n not in names]:
            record = self.discover_workers().get(name)
            if record and record.available:
                return name
        return None

    def record_worker_history(
        self,
        task: AutoFixTask | None,
        *,
        worker: str,
        status: str,
        reason: str | None = None,
        timestamp: str | None = None,
        subtask: str | None = None,
    ) -> None:
        """Persist one worker attempt on the task record immediately."""
        if task is None:
            return
        try:
            history = getattr(task, "worker_history")
        except AttributeError:
            task.worker_history = []
            history = task.worker_history
        entry = {
            "subtask": subtask,
            "worker": worker,
            "status": status,
            "reason": reason or "",
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        }
        history.append(entry)
        try:
            task.save()
        except Exception:
            pass

    def _run_worker(self, name: str, prompt: str, workspace: str, on_output=None, timeout: int | None = None) -> CodingResult:
        worker = self._create_worker(name)
        if worker is None:
            return CodingResult(
                backend=name,
                success=False,
                error=f"Unknown worker configuration: {name}",
                started=False,
                worker_name=name,
            )
        return worker.execute(prompt, workspace, on_output=on_output, timeout=timeout)

    def execute(
        self,
        prompt: str,
        workspace: str | Path,
        *,
        task: AutoFixTask | None = None,
        subtask_id: str | None = None,
        on_output=None,
        timeout: int | None = None,
        prefer: str | None = None,
    ) -> CodingResult:
        """Execute ONE unit of work with automatic same-subtask failover."""
        if isinstance(workspace, Path):
            workspace = str(workspace)

        if timeout is None:
            timeout = int(self.env.get("AUTOFIX_WORKER_TIMEOUT", _DEFAULT_WORKER_TIMEOUT) or _DEFAULT_WORKER_TIMEOUT)
        if timeout <= 0:
            timeout = None  # no timeout

        # Strict configured-priority order (preferred worker hoisted). Workers
        # that are unavailable are recorded as such in the SAME subtask's
        # history before the next one runs — the sequence stays auditable.
        candidates = list(self.priority)
        if prefer and prefer in candidates:
            candidates.remove(prefer)
            candidates.insert(0, prefer)

        #: Canonical outcome per worker observed during THIS routing attempt —
        #: used for the consolidated all-workers-failed notification only.
        attempted: dict[str, str] = {}

        for name in candidates:
            record = self.discover_workers().get(name)
            if record is None or not record.available:
                attempted[name] = WORKER_UNAVAILABLE
                self.record_worker_history(
                    task,
                    worker=name,
                    status=HISTORY_STATUS[WORKER_UNAVAILABLE],
                    reason=(record.detail if record else "unknown worker"),
                    subtask=subtask_id,
                )
                self._emit_notification(worker_notification(name, WORKER_UNAVAILABLE))
                continue

            record.attempts += 1
            record.last_status = "starting"
            try:
                result = self._run_worker(name, prompt, workspace, on_output=on_output, timeout=timeout)
                result.worker_name = result.worker_name or name
            except Exception as exc:  # defensive: a crashing adapter must not kill AutoFix
                record.failures += 1
                record.last_status = HISTORY_STATUS[WORKER_EXECUTION_ERROR]
                attempted[name] = WORKER_EXECUTION_ERROR
                self.record_worker_history(
                    task,
                    worker=name,
                    status=HISTORY_STATUS[WORKER_EXECUTION_ERROR],
                    reason=f"worker raised: {exc}",
                    subtask=subtask_id,
                )
                continue

            outcome = classify_worker_result(result)
            record.last_status = HISTORY_STATUS[outcome]
            attempted[name] = outcome

            if outcome == WORKER_SUCCESS:
                record.healthy = True
                self.record_worker_history(
                    task,
                    worker=name,
                    status=HISTORY_STATUS[outcome],
                    reason="task completed",
                    subtask=subtask_id,
                )
                return result

            record.failures += 1
            reason = result.error or result.summary
            if outcome in {
                WORKER_UNAVAILABLE,
                WORKER_AUTHENTICATION_ERROR,
                WORKER_INVALID_CONFIGURATION,
                WORKER_QUOTA_EXCEEDED,
            }:
                # Persistently unusable until refresh_workers() re-evaluates.
                record.available = False
                record.configured = False
                record.detail = reason
            # Emit observability notification for actionable outcomes.
            # AutoFix continues to the next worker regardless.
            self._emit_notification(worker_notification(name, outcome))
            # TIMEOUT and EXECUTION_ERROR stay available (transient), but the
            # state below is persisted BEFORE the next worker is attempted.
            self.record_worker_history(
                task,
                worker=name,
                status=HISTORY_STATUS[outcome],
                reason=reason,
                subtask=subtask_id,
            )

        fallback = CodingResult(
            backend=None,
            success=False,
            error=(
                f"{NO_AVAILABLE_WORKER_MESSAGE} "
                f"Tried workers: {', '.join(candidates)}. "
                "The AutoFix task remains incomplete and can be recovered once "
                "a worker becomes available."
            ),
            started=False,
            worker_name=None,
            fallback_reason="all_workers_unavailable",
        )
        self.record_worker_history(
            task,
            worker="router",
            status="failed",
            reason=NO_AVAILABLE_WORKER_MESSAGE,
            subtask=subtask_id,
        )
        # One consolidated user-facing failure notification covering every
        # worker attempt made during this routing call.
        self._emit_notification(no_worker_available_notification(attempted))
        return fallback


def discover_worker_status(env=None) -> dict[str, WorkerRecord]:
    return WorkerRouter(env=env).discover_workers()
