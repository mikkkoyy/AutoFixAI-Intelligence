"""Safe, structured worker notifications for the AutoFix UI layer.

An observability-only bridge between the internal WorkerRouter and the
MainWindow notification surfaces (chat transcript system messages, status
bar, output panel).  This is NOT an execution path — it can never influence
routing, fallback, verification or completion.

Security rule: a WorkerNotification carries ONLY safe metadata (worker name,
event type, severity, pre-composed human text).  Raw error output, reasons,
API keys, tokens, headers or credential material are deliberately never
included.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Canonical router outcomes this module translates (single source of truth
# remains app.agents.worker_router).
EVENT_AUTHENTICATION_REQUIRED = "authentication_required"
EVENT_CONFIGURATION_REQUIRED = "configuration_required"
EVENT_UNAVAILABLE = "unavailable"
EVENT_NO_WORKER_AVAILABLE = "no_worker_available"
EVENT_QUOTA_EXCEEDED = "quota_exceeded"
EVENT_TIMEOUT = "worker_timeout"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

#: User-facing worker display names (workers stay silent/internal otherwise).
WORKER_DISPLAY_NAMES = {
    "opencode": "OpenCode",
    "deepseek": "DeepSeek",
    "copilot": "GitHub Copilot",
}

#: Short names for the consolidated worker-status list.
_SHORT_NAMES = {
    "opencode": "OpenCode",
    "deepseek": "DeepSeek",
    "copilot": "Copilot",
    "ollama": "Ollama",
}

#: Per-worker human messages for each event type. No credential ever appears.
_MESSAGES = {
    "opencode": {
        EVENT_AUTHENTICATION_REQUIRED: "OpenCode authentication required.",
        EVENT_CONFIGURATION_REQUIRED: "OpenCode configuration required.",
        EVENT_UNAVAILABLE: "OpenCode is unavailable.",
        EVENT_QUOTA_EXCEEDED: "OpenCode rate limit or quota exceeded.",
        EVENT_TIMEOUT: "OpenCode worker timed out.",
    },
    "deepseek": {
        EVENT_AUTHENTICATION_REQUIRED: "DeepSeek API key required.",
        EVENT_CONFIGURATION_REQUIRED: "DeepSeek configuration required.",
        EVENT_UNAVAILABLE: "DeepSeek is unavailable.",
        EVENT_QUOTA_EXCEEDED: "DeepSeek rate limit or quota exceeded.",
        EVENT_TIMEOUT: "DeepSeek worker timed out.",
    },
    "copilot": {
        EVENT_AUTHENTICATION_REQUIRED: "GitHub Copilot authentication required.",
        EVENT_CONFIGURATION_REQUIRED: "GitHub Copilot configuration required.",
        EVENT_UNAVAILABLE: "GitHub Copilot is unavailable.",
        EVENT_QUOTA_EXCEEDED: "GitHub Copilot rate limit or quota exceeded.",
        EVENT_TIMEOUT: "GitHub Copilot worker timed out.",
    },
    "ollama": {
        EVENT_UNAVAILABLE: "Ollama is unavailable (local fallback).",
        EVENT_TIMEOUT: "Ollama worker timed out.",
    },
}

#: Short status labels used in the consolidated worker-status list.
_STATUS_LABELS = {
    "AUTHENTICATION_ERROR": {
        "deepseek": "API key required",
        "_default": "authentication required",
    },
    "INVALID_CONFIGURATION": "configuration required",
    "UNAVAILABLE": "unavailable",
    "TIMEOUT": "timed out",
    "EXECUTION_ERROR": "execution error",
    "QUOTA_EXCEEDED": "quota/rate limit exceeded",
}


@dataclass(frozen=True)
class WorkerNotification:
    """One safe worker observability event.

    ``can_continue`` tells the UI whether AutoFix keeps running (fallback) —
    it never controls execution itself; the WorkerRouter already decided.
    """

    worker: str                 # "opencode" | "deepseek" | "copilot" | "router"
    event_type: str             # one of the EVENT_* constants
    severity: str               # info | warning | error
    message: str                # safe, pre-composed human text
    can_continue: bool          # True → fallback continues automatically
    detail: str = ""            # optional safe multi-line context (no secrets)

    def to_dict(self) -> dict:
        return asdict(self)


def _display_name(worker: str) -> str:
    return WORKER_DISPLAY_NAMES.get(worker, worker.replace("_", " ").title())


def _event_label(event: str) -> str:
    return {
        EVENT_AUTHENTICATION_REQUIRED: "authentication required",
        EVENT_CONFIGURATION_REQUIRED: "configuration required",
        EVENT_UNAVAILABLE: "is unavailable",
        EVENT_QUOTA_EXCEEDED: "rate limit or quota exceeded",
        EVENT_TIMEOUT: "worker timed out",
    }.get(event, event.replace("_", " "))


def worker_notification(worker: str, outcome_status: str) -> WorkerNotification | None:
    """Translate a canonical router outcome into a safe notification.

    Returns None for outcomes that are not user-actionable (SUCCESS, plain
    EXECUTION_ERROR crashes) to keep the UI noise low.
    """
    if outcome_status == "UNAVAILABLE":
        event, severity = EVENT_UNAVAILABLE, SEVERITY_INFO
    elif outcome_status == "AUTHENTICATION_ERROR":
        event, severity = EVENT_AUTHENTICATION_REQUIRED, SEVERITY_WARNING
    elif outcome_status == "INVALID_CONFIGURATION":
        event, severity = EVENT_CONFIGURATION_REQUIRED, SEVERITY_WARNING
    elif outcome_status == "QUOTA_EXCEEDED":
        event, severity = EVENT_QUOTA_EXCEEDED, SEVERITY_WARNING
    elif outcome_status == "TIMEOUT":
        event, severity = EVENT_TIMEOUT, SEVERITY_WARNING
    else:
        return None

    message = _MESSAGES.get(worker, {}).get(event)
    if message is None:
        message = f"{_display_name(worker)} {_event_label(event)}."
    return WorkerNotification(
        worker=worker,
        event_type=event,
        severity=severity,
        message=message,
        can_continue=True,
    )


def no_worker_available_notification(
    worker_outcomes: dict[str, str] | None = None,
) -> WorkerNotification:
    """Consolidated failure notification for the all-workers-exhausted case.

    ``worker_outcomes`` maps worker name → canonical outcome status observed
    during the routing attempt; only safe status labels are rendered.
    """
    lines = []
    for worker, outcome in (worker_outcomes or {}).items():
        table = _STATUS_LABELS.get(outcome)
        if isinstance(table, dict):
            label = table.get(worker, table["_default"])
        elif isinstance(table, str):
            label = table
        else:
            label = "unavailable"
        lines.append(f"- {_SHORT_NAMES.get(worker, worker)} — {label}")

    detail = ("Worker status:\n" + "\n".join(lines)) if lines else ""
    return WorkerNotification(
        worker="router",
        event_type=EVENT_NO_WORKER_AVAILABLE,
        severity=SEVERITY_ERROR,
        message="AutoFix could not find an available worker.",
        can_continue=False,
        detail=detail,
    )
