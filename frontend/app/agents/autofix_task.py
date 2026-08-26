"""Persistent AutoFix task object.

A coding-agent PROCESS is not a TASK: the OpenCode subprocess can exit at any
moment (crash, timeout, cancellation) while the logical AutoFix task it was
working on remains incomplete.  This module owns the durable task record that
survives process termination:

    <workspace>\\.autofix\\tasks\\autofix-task-<id>.json

Key rule enforced everywhere: ``PROCESS EXIT != TASK COMPLETION``.  Completion
is only ever decided by verification results recorded on this object.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.agents.task_transport import new_task_id, tasks_dir

# Explicit task states.  The critical distinction:
#   the OpenCode PROCESS may be gone while the TASK is still RUNNING/RECOVERING.
PENDING = "PENDING"
READY = "READY"
RUNNING = "RUNNING"
WAITING = "WAITING"
PAUSED = "PAUSED"
STOPPED = "STOPPED"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
RECOVERING = "RECOVERING"
VERIFYING = "VERIFYING"
COMPLETED = "COMPLETED"
SKIPPED = "SKIPPED"
CANCELLED = "CANCELLED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

TERMINAL_STATES = {COMPLETED, CANCELLED, FAILED, RECOVERY_REQUIRED}

DEFAULT_MAX_RECOVERY_ATTEMPTS = 3


def max_recovery_attempts(env=None) -> int:
    """``AUTOFIX_MAX_RECOVERY_ATTEMPTS`` — hard bound on continuation tries."""
    env = os.environ if env is None else env
    raw = str(env.get("AUTOFIX_MAX_RECOVERY_ATTEMPTS", "")).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_RECOVERY_ATTEMPTS
    return value if value >= 0 else DEFAULT_MAX_RECOVERY_ATTEMPTS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskSubtask:
    """One unit of work inside a larger AutoFix task."""

    id: str
    title: str
    description: str
    dependencies: list[str] = field(default_factory=list)
    assigned_agent: str = "coder"
    status: str = PENDING
    result: str | None = None
    error: str | None = None
    verification: str | None = None
    worker: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    verified: bool | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSubtask":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _agent_for_subtask(title: str, description: str) -> str:
    """Select the best existing AutoFix agent for a decomposed task."""
    text = f"{title} {description}".lower()
    if any(token in text for token in ("test", "verify", "assert", "coverage")):
        return "tester"
    if any(token in text for token in ("review", "refactor", "quality", "lint", "docs")):
        return "reviewer"
    if any(token in text for token in ("debug", "error", "bug", "broken", "trace", "fix")):
        return "debugger"
    if any(token in text for token in ("architecture", "inspect", "analyze", "plan", "design")):
        return "planner"
    return "coder"


def decompose_request(description: str, plan_text: str | None = None) -> list[TaskSubtask]:
    """Create a dependency-aware decomposition for a high-level AutoFix request."""
    source = (description or "").strip()
    plan_hint = (plan_text or "").strip()
    text = f"{source}\n{plan_hint}".strip()
    lowered = text.lower()

    file_names = []
    if "file" in lowered or "files" in lowered:
        matches = re.findall(r"[A-Za-z0-9_.-]+\.(?:txt|py|md|json|js|ts|css|yaml|yml)", text)
        file_names = list(dict.fromkeys(matches))[:6]

    if len(file_names) >= 3:
        subtasks = [
            TaskSubtask(
                id=f"subtask-01",
                title=f"Create {file_names[0]}",
                description=f"Create {file_names[0]} with the required content and ensure it matches the request.",
                dependencies=[],
                assigned_agent=_agent_for_subtask(f"Create {file_names[0]}", "Create file content"),
            )
        ]
        for index, name in enumerate(file_names[1:], start=2):
            subtasks.append(
                TaskSubtask(
                    id=f"subtask-{index:02d}",
                    title=f"Create {name}",
                    description=f"Create {name} and ensure it matches the requested output.",
                    dependencies=[subtasks[-1].id],
                    assigned_agent=_agent_for_subtask(f"Create {name}", "Create file content"),
                )
            )
        subtasks.append(
            TaskSubtask(
                id=f"subtask-{len(subtasks)+1:02d}",
                title="Verify outputs",
                description="Verify all created files and ensure the task requirements are satisfied.",
                dependencies=[st.id for st in subtasks[:-1]],
                assigned_agent="tester",
            )
        )
        return subtasks

    base_steps = []
    if any(token in lowered for token in ("inspect", "analyze", "review", "architecture", "audit")):
        base_steps.append(("Inspect current implementation", "Assess the existing architecture and identify the precise changes required."))
    if any(token in lowered for token in ("implement", "add", "create", "build", "introduce", "refactor")):
        base_steps.append(("Implement the requested change", "Implement the requested code changes in the active workspace with the necessary project context."))
    if any(token in lowered for token in ("test", "verify", "validation", "check")):
        base_steps.append(("Validate with tests", "Run the relevant validation steps and confirm the requested behavior is working."))
    if not base_steps:
        base_steps = [
            ("Inspect the request", "Review the task and understand the required change."),
            ("Implement the requested change", "Apply the relevant project changes in the active workspace."),
            ("Verify the result", "Run verification to confirm the request is satisfied."),
        ]

    subtasks = []
    for index, (title, description) in enumerate(base_steps, start=1):
        subtasks.append(
            TaskSubtask(
                id=f"subtask-{index:02d}",
                title=title,
                description=description,
                dependencies=[subtasks[-1].id] if subtasks else [],
                assigned_agent=_agent_for_subtask(title, description),
            )
        )

    if len(subtasks) > 2:
        last = subtasks[-1]
        last.dependencies = [st.id for st in subtasks[:-1]]
    return subtasks


@dataclass
class AutoFixTask:
    """Durable state for ONE logical AutoFix request."""

    task_id: str
    original_request: str
    workspace: str
    approved_prompt: str | None = None
    status: str = PENDING
    plan: str | None = None
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    remaining_steps: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    test_results: list[dict] = field(default_factory=list)
    opencode_session: str | None = None
    recovery_attempts: int = 0
    termination_reason: str | None = None
    last_output_tail: str | None = None
    #: Tails of PREVIOUS attempts — earlier stops are never silently lost.
    output_history: list[str] = field(default_factory=list)
    #: Worker history for internal failover tracking (OpenCode, DeepSeek, Copilot).
    worker_history: list[dict] = field(default_factory=list)
    last_error: str | None = None
    verified: bool | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    completed_at: str | None = None
    subtasks: list[TaskSubtask] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    agent_assignments: dict[str, str] = field(default_factory=dict)
    verification: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def file_path(self) -> Path:
        return tasks_dir(self.workspace) / f"{self.task_id}.json"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["subtasks"] = [asdict(subtask) for subtask in self.subtasks]
        data["kind"] = "autofix-task"
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AutoFixTask":
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416
        payload = {k: v for k, v in data.items() if k in known}
        subtasks = payload.get("subtasks", [])
        if isinstance(subtasks, list):
            payload["subtasks"] = [
                TaskSubtask.from_dict(subtask) if isinstance(subtask, dict) else subtask
                for subtask in subtasks
            ]
        return cls(**payload)

    def decompose(
        self,
        *,
        description: str | None = None,
        plan_text: str | None = None,
        force: bool = False,
    ) -> list[TaskSubtask]:
        """Generate and persist a dependency-aware task decomposition.

        Idempotent: a task that already has subtasks (same-task recovery or
        resume) keeps its existing decomposition, statuses and worker history
        unless ``force`` regenerates it explicitly.
        """
        if self.subtasks and not force:
            for subtask in self.subtasks:
                if subtask.assigned_agent:
                    self.agent_assignments[subtask.id] = subtask.assigned_agent
                self.dependencies[subtask.id] = list(subtask.dependencies)
            if not self.verification:
                self.verification = {
                    "status": "pending",
                    "required": True,
                    "summary": "Top-level completion requires successful verification.",
                }
            self.save()
            return self.subtasks

        target = description or self.original_request or self.approved_prompt or self.plan or ""
        subtasks = decompose_request(target, plan_text or self.plan)
        for subtask in subtasks:
            if subtask.assigned_agent:
                self.agent_assignments[subtask.id] = subtask.assigned_agent
            self.dependencies[subtask.id] = list(subtask.dependencies)
        self.subtasks = subtasks
        self.verification = {
            "status": "pending",
            "required": True,
            "summary": "Top-level completion requires successful verification.",
        }
        self.save()
        return self.subtasks

    def apply_decomposition(self, *, description: str | None = None, plan_text: str | None = None):
        return self.decompose(description=description, plan_text=plan_text)

    def subtask_by_id(self, subtask_id: str) -> TaskSubtask | None:
        for subtask in self.subtasks:
            if subtask.id == subtask_id:
                return subtask
        return None

    def ready_subtasks(self) -> list[TaskSubtask]:
        ready: list[TaskSubtask] = []
        by_id = {subtask.id: subtask for subtask in self.subtasks}
        for subtask in self.subtasks:
            if subtask.status in {COMPLETED, FAILED, SKIPPED}:
                continue
            dependency_ids = list(subtask.dependencies or [])
            if not dependency_ids:
                ready.append(subtask)
                continue
            if all(by_id.get(dep_id) is not None and by_id[dep_id].status == COMPLETED for dep_id in dependency_ids):
                ready.append(subtask)
        return ready

    def unfinished_subtasks(self) -> list[TaskSubtask]:
        return [
            subtask for subtask in self.subtasks
            if subtask.status not in {COMPLETED, FAILED, SKIPPED}
        ]

    def save(self) -> Path:
        self.updated_at = _now_iso()
        path = self.file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, workspace: str | Path, task_id: str) -> "AutoFixTask | None":
        path = tasks_dir(workspace) / f"{task_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return cls.from_dict(data)

    @classmethod
    def create(cls, workspace: str | Path, original_request: str) -> "AutoFixTask":
        task_id = new_task_id(original_request, prefix="autofix-task")
        task = cls(
            task_id=task_id,
            original_request=original_request,
            workspace=str(workspace),
            approved_prompt=original_request,
        )
        task.save()
        return task

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition(self, status: str) -> None:
        self.status = status
        if status == COMPLETED and self.completed_at is None:
            self.completed_at = _now_iso()
        self.save()

    def note_stage(self, stage: str, ok: bool, message: str = "") -> None:
        entry = {"stage": stage, "ok": ok, "message": message.splitlines()[0] if message else ""}
        label = f"{stage}: {'ok' if ok else 'failed'}" + (
            f" — {entry['message']}" if not ok and entry["message"] else ""
        )
        if ok:
            if label not in self.completed_steps:
                self.completed_steps.append(label)
        else:
            self.diagnostics.append(entry["message"] or stage)
        self.current_step = stage
        self.save()

    def remaining_work(self) -> str:
        if self.remaining_steps:
            return "; ".join(self.remaining_steps)
        parts = []
        if self.recovery_attempts:
            parts.append(f"recovery attempts used: {self.recovery_attempts}")
        if self.diagnostics:
            parts.append("recent problems: " + "; ".join(self.diagnostics[-3:]))
        if self.termination_reason:
            parts.append(f"last stop reason: {self.termination_reason}")
        return (
            "Finish and verify the ORIGINAL request in full. "
            + ("; ".join(parts) if parts else "")
        )

    # ------------------------------------------------------------------
    # Recovery helpers
    # ------------------------------------------------------------------

    def output_tail(self, lines: int = 40) -> str:
        if not self.last_output_tail:
            return "(no captured output)"
        kept = self.last_output_tail.splitlines()[-lines:]
        return "\n".join(kept)

    def push_output_tail(self, tail: str, history_cap: int = 5) -> None:
        """Record a new output tail, keeping recent previous ones."""
        if self.last_output_tail:
            self.output_history.append(self.last_output_tail)
            del self.output_history[:-history_cap]
        self.last_output_tail = tail or None


_SESSION_ID_RE = re.compile(
    r"\bsession[:\s]+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[a-z0-9]{16,})",
    re.IGNORECASE,
)


def extract_session_id(output: str) -> str | None:
    """Best-effort OpenCode session-id extraction from captured output.

    Only well-formed identifiers are accepted; nothing is invented.
    """
    match = _SESSION_ID_RE.search(output or "")
    return match.group(1) if match else None


def build_continuation_context(
    task: AutoFixTask,
    *,
    termination_reason: str | None = None,
    extra_notes: list[str] | None = None,
) -> str:
    """Compact recovery prompt continuing the SAME logical task.

    Contains everything needed to resume: original request, plan, progress,
    project state hints, previous output tail and stop reason.  Never a bare
    ``continue``.
    """
    reason = termination_reason or task.termination_reason or "unexpected_stop"
    sections = [
        "You are continuing an existing AutoFix task.",
        f"Task ID:\n{task.task_id}",
        f"Original request:\n{task.original_request}",
        f"Original plan:\n{task.plan or '(no explicit plan)'}",
        "Completed:\n"
        + ("\n".join(f"- {step}" for step in task.completed_steps) or "- (nothing confirmed yet)"),
        "Remaining:\n" + task.remaining_work(),
        "Files changed so far:\n"
        + ("\n".join(task.files_changed) or "(none recorded)"),
        "Diagnostics:\n"
        + ("\n".join(task.diagnostics[-5:]) or "(none)"),
        f"Previous termination reason:\n{reason}",
        "Previous agent output (tail):\n" + task.output_tail(),
    ]
    if task.opencode_session:
        sections.append(f"Previous session id (continue it if supported):\n{task.opencode_session}")
    if extra_notes:
        sections.append("Notes:\n" + "\n".join(extra_notes))
    sections.append(
        "The current workspace state is authoritative. Do not restart "
        "completed work. Continue the remaining task from the current "
        "project state. After completing the remaining work, verify it."
    )
    return "\n\n".join(sections)
