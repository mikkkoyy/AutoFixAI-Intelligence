"""Qt thread workers for the AI Chat approval workflow.

``PlanWorker``      — analyzes the workspace and produces a plan (never executes).
``ApprovalPipeline``— executes the approved plan:
                        Planner → Coding Agent (OpenCode) → Tester
                        → Debugger (if needed) → Reviewer → Verification,
                        with same-task recovery when OpenCode stops early.

Core rule: ``PROCESS EXIT != TASK COMPLETION``.  The coding agent's exit code
is only a process event — completion is decided by verification results and
recorded on a persistent AutoFix task that survives process termination.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.agents.autofix_task import (
    AutoFixTask,
    BLOCKED,
    CANCELLED,
    COMPLETED,
    FAILED,
    PENDING,
    READY,
    RECOVERING,
    RECOVERY_REQUIRED,
    RUNNING,
    SKIPPED,
    VERIFYING,
    extract_session_id,
    max_recovery_attempts,
)
from app.agents.coding_agent import CodingAgentRunner, CodingResult
from app.agents.orchestrator import (
    DebuggerAgent,
    PlannerAgent,
    RecoveryAgent,
    ReviewerAgent,
    TesterAgent,
    VerificationAgent,
)
from app.agents.task_memory import KIND_ERRORS, KIND_FIXES, KIND_SESSIONS, record_memory
from app.agents.worker_router import NO_AVAILABLE_WORKER_MESSAGE, WorkerRouter
from app.core.models import AgentStatus, AgentTask


class PlanWorker(QThread):
    """Produces a plan for a chat request without executing anything.

    When a cloud provider is configured (OPENAI_API_KEY etc.) the plan comes
    from the shared provider layer (``app.agents.chat_provider.analyze``);
    otherwise — or when the provider fails — the deterministic local
    planner produces the plan and any provider error is reported honestly.
    """

    plan_ready = Signal(str)
    plan_failed = Signal(str)

    def __init__(self, description: str, workspace: str, parent=None):
        super().__init__(parent)
        self._description = description
        self._workspace = workspace
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            from app.agents.chat_provider import analyze

            provider_plan, source = analyze(self._description, self._workspace)
            if self._cancelled:
                return
            if provider_plan:
                self.plan_ready.emit(
                    f"[plan source: {source}]\n\n{provider_plan}"
                )
                return
            if source:
                # A provider was configured but failed — say so, then fall
                # back to the honest local planner instead of going silent.
                self.plan_ready.emit(
                    f"[cloud provider unavailable — local planner used]\n{source}"
                )
        except ImportError:
            pass
        except Exception as exc:
            if not self._cancelled:
                self.plan_failed.emit(f"Provider planning error: {exc}")
                return

        try:
            task = AgentTask(description=self._description, workspace=self._workspace)
            result = PlannerAgent().run(task, {})
            if self._cancelled:
                return
            if result.status == AgentStatus.FAILED:
                self.plan_failed.emit(result.message)
            else:
                message = result.message
                guidance = self._shared_guidance()
                if guidance:
                    message = message + "\n\n" + guidance
                intel = self._intelligence_guidance()
                if intel:
                    message = message + "\n\n" + intel
                self.plan_ready.emit(message)
        except Exception as exc:
            if not self._cancelled:
                self.plan_failed.emit(f"Planner error: {exc}")

    def _shared_guidance(self) -> str:
        """Relevant shared AI-knowledge guidance for the plan (optional).

        Priority-ordered context: project files/config and project memory
        always outrank this block; retrieval failures stay silent.
        """
        try:
            from app.agents.github_knowledge import shared_knowledge_block

            return shared_knowledge_block(self._description, limit=2)
        except Exception:
            return ""

    def _intelligence_guidance(self) -> str:
        """Relevant AI intelligence context for the plan (optional).

        Retrieves validated, approved intelligence from the Intelligence
        Storage that is relevant to the task description.  Empty when no
        relevant intelligence is found or when IntelligenceManager is
        unavailable.  Failures stay silent.
        """
        try:
            from app.agents.intelligence_manager import IntelligenceManager

            mgr = IntelligenceManager(self._workspace)
            return mgr.build_intelligence_context(self._description, max_chars=1200)
        except Exception:
            return ""


#: How much coding-agent output is kept on the persistent task record.
_OUTPUT_TAIL_CHARS = 12000

_SNAPSHOT_SKIP_DIRS = {".autofix", ".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


def _snapshot_workspace(workspace: str | Path, limit: int = 5000) -> dict[str, float]:
    """Cheap mtime snapshot used to detect files changed by the agent."""
    root = Path(workspace)
    snapshot: dict[str, float] = {}
    try:
        for path in root.rglob("*"):
            if any(part in _SNAPSHOT_SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                try:
                    snapshot[str(path.relative_to(root))] = path.stat().st_mtime
                except OSError:
                    continue
            if len(snapshot) >= limit:
                break
    except OSError:
        pass
    return snapshot


def _changed_files(before: dict[str, float], after: dict[str, float]) -> list[str]:
    changed = [
        name for name, mtime in after.items()
        if before.get(name) != mtime
    ]
    return sorted(changed)[:200]


class ApprovalPipeline(QThread):
    """Executes an approved plan end-to-end and reports honest results."""

    stage_started = Signal(str)
    stage_finished = Signal(str, bool, str)
    coding_output = Signal(str)
    status_changed = Signal(str)
    #: Safe worker notifications (authentication/configuration problems).
    #: Payload is a dict from WorkerNotification.to_dict() — metadata only.
    worker_notification = Signal(dict)
    pipeline_finished = Signal(bool, str)

    MAX_DEBUG_CYCLES = 1

    def __init__(
        self,
        description: str,
        workspace: str,
        coding_runner: CodingAgentRunner | None = None,
        approved_plan: str | None = None,
        existing_task: AutoFixTask | None = None,
        context_metadata: dict | None = None,
        worker_router: WorkerRouter | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._description = description
        self._workspace = workspace
        self._approved_plan = approved_plan
        #: Pre-created durable task (e.g. from a Chat handoff) — reused as-is
        #: so there is exactly ONE logical task per request.
        self._existing_task = existing_task
        #: Extra routing metadata (intent category, referenced files …) that
        #: travels with every coding-agent payload for context continuity.
        self.context_metadata = dict(context_metadata or {})
        self._coding_runner = coding_runner or CodingAgentRunner()
        #: Single worker-routing authority (injectable for deterministic tests).
        self._worker_router = worker_router or WorkerRouter()
        self._cancelled = False
        self.backend_used: str | None = None
        #: Persistent task object — survives OpenCode process termination.
        self.autofix_task: AutoFixTask | None = None
        #: Final task state: COMPLETED / FAILED / CANCELLED / RECOVERY_REQUIRED.
        self.final_state: str | None = None
        self._recovery_agent = RecoveryAgent()

    def cancel(self):
        """User cancellation — kills the agent process; NEVER auto-restarts."""
        self._cancelled = True
        kill = getattr(self._coding_runner, "cancel_active", None)
        if callable(kill):
            try:
                kill()
            except Exception:
                pass

    # ------------------------------------------------------------------

    def run(self):
        try:
            self._execute()
        except Exception as exc:
            self.pipeline_finished.emit(False, f"Pipeline error: {exc}")

    def _status(self, text: str):
        self.status_changed.emit(text)

    def _emit_worker_notification(self, notification):
        to_dict = getattr(notification, "to_dict", None)
        payload = to_dict() if callable(to_dict) else dict(notification)
        try:
            self.worker_notification.emit(payload)
        except Exception:
            pass

    def _execute(self):
        # Observability bridge: router notifications (safe metadata only) are
        # re-emitted as a Qt signal for the UI. They never affect execution.
        self._worker_router.on_notification = self._emit_worker_notification
        if self._existing_task is not None:
            task_obj = self._existing_task
        else:
            task_obj = AutoFixTask.create(self._workspace, self._description)
        if self._approved_plan:
            task_obj.plan = self._approved_plan
        task_obj.approved_prompt = self._description
        task_obj.transition(RUNNING)
        self.autofix_task = task_obj

        context: dict = {"workspace": self._workspace, "autofix_task": task_obj}
        if task_obj.subtasks:
            # Same-task recovery/resume — keep the EXISTING decomposition so
            # completed subtasks are never duplicated or renumbered. Only
            # stale retryable states go back to PENDING.
            context["subtasks"] = task_obj.subtasks
            context["dependencies"] = task_obj.dependencies
            context["agent_assignments"] = task_obj.agent_assignments
            self._reset_stale_subtasks(task_obj)
        else:
            task_obj.decompose(description=self._description, plan_text=self._approved_plan or task_obj.plan)
            context["subtasks"] = task_obj.subtasks
            context["dependencies"] = task_obj.dependencies
            context["agent_assignments"] = task_obj.agent_assignments
        max_attempts = max_recovery_attempts()

        self._record_session("running")

        # 1 ── Planner -------------------------------------------------
        self._status("AutoFix: Planning")
        self.stage_started.emit("Planner")
        planner = PlannerAgent().run(
            AgentTask(description=self._description, workspace=self._workspace), context
        )
        task_obj.note_stage("Planner", planner.status == AgentStatus.PASSED, planner.message)
        if task_obj.plan is None:
            task_obj.plan = planner.message
        self.stage_finished.emit("Planner", planner.status == AgentStatus.PASSED, planner.message)

        prompt = self._description
        attempts_used = 0
        last_error: str | None = None

        if task_obj.subtasks:
            while True:
                subtask_success = self._execute_subtasks(task_obj, context)
                if not subtask_success:
                    if self._cancelled:
                        self._finish_state(CANCELLED, "Cancelled by user.")
                        return
                    last_error = task_obj.last_error or "subtask execution failed"
                    if self._is_no_worker_failure(last_error):
                        self._finish_state(FAILED, f"Execution FAILED — no code changes were verified.\n{last_error}")
                        return
                    if attempts_used >= max_attempts:
                        self._finish_recovery_required(task_obj, last_error, attempts_used)
                        return
                    attempts_used += 1
                    task_obj.recovery_attempts = attempts_used
                    task_obj.termination_reason = "unexpected_stop"
                    task_obj.last_error = last_error
                    task_obj.transition(RECOVERING)
                    self._record_error(task_obj, last_error, attempts_used)
                    self._status("AutoFix: Recovering\nOpenCode: Continuing")
                    self.stage_started.emit("Recovery")
                    continuation = self._recovery_agent.run(
                        AgentTask(description=self._description, workspace=self._workspace),
                        {"autofix_task": task_obj},
                    )
                    prompt = continuation.message
                    self.stage_finished.emit("Recovery", True, f"Continuing same task (attempt {attempts_used})")
                    # Same task, same subtasks — but worker availability may
                    # have changed, so re-evaluate before continuing.
                    self._worker_router.refresh_workers()
                    recovery_result = self._run_coding(prompt)
                    if recovery_result.success:
                        # Continuation ran — but completion still requires
                        # SUBTASK VERIFICATION. Never mark unverified work done.
                        for subtask in task_obj.subtasks:
                            if subtask.status in {COMPLETED, SKIPPED}:
                                continue
                            if recovery_result.output:
                                subtask.result = recovery_result.output
                            subtask.status = VERIFYING
                            subtask.verified = self._verify_subtask(task_obj, subtask, context)
                            if subtask.verified:
                                subtask.status = COMPLETED
                                subtask.completed_at = str(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
                                task_obj.completed_steps.append(f"Subtask {subtask.id}: {subtask.title}")
                            else:
                                subtask.status = FAILED
                                subtask.error = "Subtask verification failed after recovery."
                        task_obj.save()
                        complete = self._verify_work(task_obj, context)
                        if self._cancelled:
                            self._finish_state(CANCELLED, "Cancelled by user.")
                            return
                        if complete:
                            self._finish_completed(task_obj)
                            return
                        last_error = context.get("_incomplete_reason", "verification failed")
                        if attempts_used >= max_attempts:
                            self._finish_recovery_required(task_obj, last_error, attempts_used)
                            return
                        continue
                    continue
                complete = self._verify_work(task_obj, context)
                if self._cancelled:
                    self._finish_state(CANCELLED, "Cancelled by user.")
                    return
                if complete:
                    self._finish_completed(task_obj)
                    return
                last_error = context.get("_incomplete_reason", "verification failed")
                if attempts_used >= max_attempts:
                    self._finish_recovery_required(task_obj, last_error, attempts_used)
                    return
                attempts_used += 1
                task_obj.recovery_attempts = attempts_used
                task_obj.termination_reason = "unexpected_stop"
                task_obj.last_error = last_error
                task_obj.transition(RECOVERING)
                self._record_error(task_obj, last_error, attempts_used)
                self._status("AutoFix: Recovering\nOpenCode: Continuing")
                self.stage_started.emit("Recovery")
                continuation = self._recovery_agent.run(
                    AgentTask(description=self._description, workspace=self._workspace),
                    {"autofix_task": task_obj},
                )
                prompt = continuation.message
                self.stage_finished.emit("Recovery", True, f"Continuing same task (attempt {attempts_used})")
                # Worker availability may have changed — re-evaluate on resume.
                self._worker_router.refresh_workers()

        while True:
            # ── Coding agent (OpenCode) -------------------------------
            self._status("AutoFix: Executing\nOpenCode: Running")
            self.stage_started.emit("Coding")
            self.coding_output.emit(f"$ coding agent: {prompt.splitlines()[0]} …")
            before = _snapshot_workspace(self._workspace)
            coding = self._run_coding(prompt)
            after = _snapshot_workspace(self._workspace)
            task_obj.files_changed = sorted(
                set(task_obj.files_changed) | set(_changed_files(before, after))
            )
            self.backend_used = coding.backend or self.backend_used
            last_error = coding.summary
            task_obj.note_stage("Coding", coding.success, coding.summary)
            self.stage_finished.emit("Coding", coding.success, coding.summary)

            if self._cancelled:
                self._finish_state(CANCELLED, "Cancelled by user.")
                return

            if not coding.success and coding.backend is None:
                # No backend could even run — nothing to recover from.
                self._finish_state(
                    FAILED,
                    "Execution FAILED — no code changes were verified.\n"
                    f"{coding.summary}",
                )
                return

            if coding.success:
                complete = self._verify_work(task_obj, context)
                if self._cancelled:
                    self._finish_state(CANCELLED, "Cancelled by user.")
                    return
                if complete:
                    self._finish_completed(task_obj)
                    return
                last_error = context.get("_incomplete_reason", "verification failed")
            else:
                last_error = coding.summary

            # ── Same-task recovery decision ----------------------------
            if attempts_used >= max_attempts:
                self._finish_recovery_required(task_obj, last_error, attempts_used)
                return

            attempts_used += 1
            task_obj.recovery_attempts = attempts_used
            task_obj.termination_reason = "unexpected_stop"
            task_obj.last_error = last_error
            task_obj.transition(RECOVERING)
            self._record_error(task_obj, last_error, attempts_used)
            self._status("AutoFix: Recovering\nOpenCode: Continuing")
            self.stage_started.emit("Recovery")
            continuation = self._recovery_agent.run(
                AgentTask(description=self._description, workspace=self._workspace),
                {"autofix_task": task_obj},
            )
            prompt = continuation.message
            self.stage_finished.emit("Recovery", True, f"Continuing same task (attempt {attempts_used})")
            # Worker availability may have changed — re-evaluate on resume.
            self._worker_router.refresh_workers()

    # ------------------------------------------------------------------

    def _is_no_worker_failure(self, error: str | None) -> bool:
        """True when the failure means 'no internal worker could run at all'."""
        text = error or ""
        return (
            NO_AVAILABLE_WORKER_MESSAGE in text
            or "No coding agent available" in text
            or "No usable internal worker" in text
        )

    def _reset_stale_subtasks(self, task_obj: AutoFixTask) -> None:
        """Same-task recovery: retryable states go back to PENDING.

        COMPLETED/SKIPPED subtasks are never touched — completed work is not
        repeated. FAILED subtasks become retryable because a previous worker
        (not the implementation) may have been the problem.
        """
        changed = False
        for subtask in task_obj.subtasks:
            if subtask.status in {READY, RUNNING, VERIFYING, FAILED}:
                subtask.status = PENDING
                changed = True
        if changed:
            task_obj.save()

    def _run_coding(self, prompt: str, subtask_id: str | None = None) -> CodingResult:
        runner = self._coding_runner
        try:
            runner.payload_context = {
                "autofix_task": (
                    self.autofix_task.file_path().name if self.autofix_task else None
                ),
                "approved_plan": (self._approved_plan or "")[:2000] or None,
            }
            if self.context_metadata:
                runner.payload_context["chat_context"] = dict(self.context_metadata)
        except Exception:
            pass

        # Tests and explicit injected runners may supply their own execution
        # implementation. Those should be respected without forcing the internal
        # worker router to run a real external backend.
        if self._coding_runner.__class__ is not CodingAgentRunner:
            self._last_run_used_router = False
            result = runner.execute(
                prompt,
                self._workspace,
                on_output=self.coding_output.emit,
            )
        else:
            # Use the internal worker router so AutoFix can fall back between
            # OpenCode, DeepSeek and Copilot without creating a separate execution
            # pipeline. The same AutoFix task and the same subtask persist across
            # fallback attempts. A router failure is reported honestly — it is
            # never silently rerouted around the WorkerRouter.
            self._last_run_used_router = True
            result = self._worker_router.execute(
                prompt,
                self._workspace,
                task=self.autofix_task,
                subtask_id=subtask_id,
                on_output=self.coding_output.emit,
                timeout=None,
            )
        if self.autofix_task is not None:
            tail = (result.output or "")[-_OUTPUT_TAIL_CHARS:]
            self.autofix_task.push_output_tail(tail)
            session_id = extract_session_id(result.output or "")
            if session_id:
                self.autofix_task.opencode_session = session_id
            if result.timed_out:
                self.autofix_task.termination_reason = "timeout"
            elif not result.started:
                self.autofix_task.termination_reason = "process_error"
            elif not result.success:
                self.autofix_task.termination_reason = "exit_code_nonzero"
            else:
                self.autofix_task.termination_reason = "completed"
            self.autofix_task.save()
        return result

    def _build_subtask_prompt(self, task_obj: AutoFixTask, subtask) -> str:
        dependency_notes = []
        for dep_id in subtask.dependencies or []:
            dependency = task_obj.subtask_by_id(dep_id)
            if dependency and dependency.result:
                dependency_notes.append(f"Dependency {dep_id} ({dependency.title}):\n{dependency.result[:800]}")
        dependency_block = "\n\n".join(dependency_notes) if dependency_notes else "(no prior dependency output captured)"
        previous_attempts = [
            entry for entry in (getattr(task_obj, "worker_history", None) or [])
            if isinstance(entry, dict) and entry.get("subtask") == subtask.id
        ]
        prior_block = ""
        if previous_attempts:
            attempt_lines = [
                f"- worker={entry.get('worker')} status={entry.get('status')}"
                + (f" ({entry.get('reason')})" if entry.get("reason") else "")
                for entry in previous_attempts[-6:]
            ]
            prior_block = (
                "\n\nPrevious worker attempts for THIS SAME subtask:\n"
                + "\n".join(attempt_lines)
                + "\nThe workspace may already contain partial changes from those "
                "attempts. Inspect the current workspace state first and continue "
                "from it; do not assume files are missing and do not undo or "
                "repeat work that is already present."
            )
        return (
            "You are continuing the same AutoFix task and must execute one subtask in the existing pipeline.\n\n"
            f"Task ID: {task_obj.task_id}\n"
            f"Original request:\n{task_obj.original_request}\n\n"
            f"Approved prompt:\n{task_obj.approved_prompt or task_obj.original_request}\n\n"
            f"Subtask ID: {subtask.id}\n"
            f"Subtask title: {subtask.title}\n"
            f"Subtask description:\n{subtask.description}\n\n"
            f"Assigned agent: {subtask.assigned_agent}\n"
            f"Dependencies: {', '.join(subtask.dependencies) if subtask.dependencies else 'none'}\n\n"
            "Dependency context:\n"
            f"{dependency_block}\n"
            f"{prior_block}\n\n"
            f"Workspace: {self._workspace}\n\n"
            "Execute only this subtask and keep the original request intact. Do not replace the task with a shorter summary."
        )

    def _verify_subtask(self, task_obj: AutoFixTask, subtask, context: dict | None = None) -> bool:
        title = (subtask.title or "").lower()
        description = (subtask.description or "").lower()
        file_names = []
        try:
            import re
            file_names = re.findall(r"[A-Za-z0-9_.-]+\.(?:txt|py|md|json|yaml|yml|js|ts|css)", f"{title} {description}")
        except Exception:
            file_names = []
        if "verify" in title or "verify" in description or "validation" in title or "validation" in description:
            for match in file_names:
                path = task_obj.file_path().parent.parent / match
                if path.exists():
                    continue
                return False
            return True
        if file_names:
            for name in file_names:
                path = Path(self._workspace) / name
                if not path.exists():
                    return False
                content = path.read_text(encoding="utf-8", errors="ignore")
                stem = name.split(".", 1)[0]
                if stem.lower() not in content.lower() and name.lower() not in content.lower():
                    return False
            return True
        if subtask.result:
            lowered = subtask.result.lower()
            if "error" in lowered or "failed" in lowered:
                return False
            return True
        return False

    def _execute_subtasks(self, task_obj: AutoFixTask, context: dict) -> bool:
        """Execute a decomposed AutoFix task one ready subtask at a time."""
        self._status("AutoFix: Executing subtasks")
        self.stage_started.emit("Coding")
        self.coding_output.emit("$ decomposed execution …")
        max_loops = max(len(task_obj.subtasks) * 2 + 5, 10)
        all_good = True
        try:
            for _ in range(max_loops):
                ready = task_obj.ready_subtasks()
                unfinished = task_obj.unfinished_subtasks()
                if not ready:
                    if not unfinished:
                        break
                    failed = [s.id for s in unfinished]
                    task_obj.last_error = f"Dependency deadlock for subtasks: {', '.join(failed)}"
                    task_obj.diagnostics.append(task_obj.last_error)
                    all_good = False
                    break

                subtask = ready[0]
                subtask.status = READY
                subtask.started_at = str(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
                subtask.worker = self._worker_router.select_worker() or "router"
                task_obj.agent_assignments[subtask.id] = subtask.assigned_agent or task_obj.agent_assignments.get(subtask.id, "coder")
                task_obj.save()

                self._status(f"AutoFix: Subtask {subtask.id}\n{subtask.title}")
                subtask.status = RUNNING
                task_obj.save()

                prompt = self._build_subtask_prompt(task_obj, subtask)
                result = self._run_coding(prompt, subtask_id=subtask.id)
                self.backend_used = result.backend or self.backend_used
                subtask.worker = result.worker_name or result.backend or subtask.worker
                subtask.result = result.output or result.error or "Subtask finished."
                subtask.error = result.error if not result.success else None
                if not getattr(self, "_last_run_used_router", False):
                    # The WorkerRouter already persisted its own attempt history
                    # (including fallback attempts); only non-router runs are
                    # recorded here to avoid duplicate entries.
                    task_obj.worker_history.append(
                        {
                            "subtask": subtask.id,
                            "worker": subtask.worker,
                            "status": "completed" if result.success else "failed",
                            "reason": subtask.error or "subtask executed",
                        }
                    )
                task_obj.save()

                if self._cancelled:
                    task_obj.last_error = "Cancelled by user while executing a subtask."
                    task_obj.termination_reason = "user_cancelled"
                    task_obj.save()
                    return False

                if not result.success:
                    # Worker-level failure (unavailable / timeout / auth /
                    # config / execution error). The router has already tried
                    # every available fallback worker on THIS SAME subtask.
                    # The subtask and the parent task stay honestly incomplete.
                    subtask.status = FAILED
                    subtask.error = result.error or result.summary
                    task_obj.last_error = subtask.error
                    task_obj.diagnostics.append(subtask.error)
                    task_obj.save()
                    all_good = False
                    break

                subtask.status = VERIFYING
                subtask.verified = self._verify_subtask(task_obj, subtask, context)
                task_obj.save()

                if not subtask.verified:
                    subtask.status = FAILED
                    subtask.error = "Subtask verification failed."
                    task_obj.last_error = subtask.error
                    task_obj.diagnostics.append(subtask.error)
                    task_obj.save()
                    all_good = False
                    break

                subtask.status = COMPLETED
                subtask.completed_at = str(__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
                subtask.verified = True
                task_obj.completed_steps.append(f"Subtask {subtask.id}: {subtask.title}")
                task_obj.save()
            else:
                task_obj.last_error = "Subtask execution loop exceeded the safety bound."
                task_obj.diagnostics.append(task_obj.last_error)
                all_good = False
        finally:
            result_message = "Decomposed execution completed successfully." if all_good else (task_obj.last_error or "Decomposed execution failed.")
            self.stage_finished.emit("Coding", all_good, result_message)

        if not all_good:
            return False
        return True

    def _verify_work(self, task_obj: AutoFixTask, context: dict) -> bool:
        """Tester → Debugger cycle → Reviewer → Verification.

        Returns True only when verification actually passes.  A zero exit
        code from the coding agent means nothing on its own.
        """
        agent_task = AgentTask(description=self._description, workspace=self._workspace)
        task_obj.transition(VERIFYING)
        self._status("AutoFix: Verifying")

        tests_ok = self._run_tests(context, task_obj)

        cycles = 0
        while tests_ok is False and cycles < self.MAX_DEBUG_CYCLES and not self._cancelled:
            cycles += 1
            self.stage_started.emit("Debugger")
            diagnosis = DebuggerAgent().run(agent_task, context)
            has_diagnosis = bool(context.get("diagnosis"))
            task_obj.note_stage(
                "Debugger", diagnosis.status == AgentStatus.PASSED, diagnosis.message
            )
            self.stage_finished.emit(
                "Debugger", diagnosis.status == AgentStatus.PASSED, diagnosis.message
            )
            if not has_diagnosis:
                break

            self._status("AutoFix: Executing\nOpenCode: Running")
            self.stage_started.emit("Coding")
            repair_prompt = (
                "The following problem was diagnosed in this project:\n"
                f"{context['diagnosis'].get('summary', 'unknown')}\n"
                f"Original request: {self._description}\n"
                "Fix the root cause so the failing tests pass."
            )
            repair = self._run_coding(repair_prompt)
            task_obj.note_stage("Coding", repair.success, repair.summary)
            self.stage_finished.emit("Coding", repair.success, repair.summary)
            if not repair.success:
                break

            tests_ok = self._run_tests(context, task_obj)

        self.stage_started.emit("Reviewer")
        review = ReviewerAgent().run(agent_task, context)
        task_obj.note_stage("Reviewer", review.status == AgentStatus.PASSED, review.message)
        self.stage_finished.emit("Reviewer", review.status == AgentStatus.PASSED, review.message)

        self.stage_started.emit("Verification")
        verification = VerificationAgent().run(agent_task, context)
        verified = verification.status == AgentStatus.PASSED
        task_obj.verified = verified
        task_obj.test_results.append({"tests_ok": tests_ok, "verified": verified})
        task_obj.note_stage("Verification", verified, verification.message)
        self.stage_finished.emit("Verification", verified, verification.message)

        success = verified and tests_ok is not False
        details = []
        if not success:
            details = [verification.message]
            if tests_ok is False:
                details.append("Test suite is still failing after the debug cycle.")
        if task_obj.subtasks:
            # Top-level completion also requires EVERY subtask to have been
            # completed. A "passing" verification must never paper over an
            # unfinished or failed subtask (no false success).
            pending_ids = [
                s.id for s in task_obj.subtasks
                if s.status not in {COMPLETED, SKIPPED}
            ]
            if pending_ids:
                success = False
                details.append(
                    "Unfinished subtasks block completion: " + ", ".join(pending_ids)
                )
        if not success:
            context["_incomplete_reason"] = "\n".join(details)
        return success

    def _run_tests(self, context: dict, task_obj: AutoFixTask) -> bool | None:
        self.stage_started.emit("Tester")
        result = TesterAgent().run(AgentTask(description="", workspace=self._workspace), context)
        ok = result.status == AgentStatus.PASSED
        task_obj.note_stage("Tester", ok, result.message)
        self.stage_finished.emit("Tester", ok, result.message)
        if "Skipping test execution" in result.message or "No test files" in result.message:
            return None
        return ok

    # ------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------

    def _finish_completed(self, task_obj: AutoFixTask):
        task_obj.verified = True
        task_obj.termination_reason = "completed"
        task_obj.transition(COMPLETED)
        self.final_state = COMPLETED
        self._record_session("completed")
        try:
            record_memory(
                self._workspace,
                KIND_FIXES,
                f"fix-{task_obj.task_id}",
                (
                    f"Request completed and verified.\n"
                    f"Files changed:\n" + "\n".join(task_obj.files_changed or ["(none tracked)"])
                ),
                tags=["autofix", task_obj.task_id],
            )
        except OSError:
            pass
        self._status("AutoFix: Completed")
        self.pipeline_finished.emit(
            True, "Execution PASSED — coding, tests and verification succeeded."
        )

    def _finish_recovery_required(
        self, task_obj: AutoFixTask, last_error: str | None, attempts_used: int
    ):
        task_obj.termination_reason = "recovery_limit_reached"
        task_obj.last_error = last_error
        task_obj.remaining_steps = [task_obj.remaining_work()]
        task_obj.transition(RECOVERY_REQUIRED)
        self.final_state = RECOVERY_REQUIRED
        self._record_session("recovery_required")
        self._record_error(task_obj, last_error or "unknown", attempts_used)

        message = (
            "AutoFix could not automatically continue this task.\n\n"
            "Completed:\n"
            + ("\n".join(f"- {s}" for s in task_obj.completed_steps) or "- (nothing confirmed)")
            + "\n\nRemaining:\n- "
            + task_obj.remaining_work()
            + f"\n\nLast error:\n{last_error or 'unknown'}\n\n"
            f"Recovery attempts: {attempts_used} (limit "
            f"{max_recovery_attempts()})\nTask state saved:\n{task_obj.file_path()}"
        )
        self._status("AutoFix: Recovery Required")
        self.pipeline_finished.emit(False, message)

    def _finish_state(self, state: str, detail: str):
        if self.autofix_task is not None:
            self.autofix_task.termination_reason = (
                "user_cancelled" if state == CANCELLED else "failed"
            )
            self.autofix_task.transition(state)
            self._record_session(state.lower())
        self.final_state = state
        if state == CANCELLED:
            self._status("AutoFix: Cancelled")
            self.pipeline_finished.emit(
                False,
                "AutoFix: Cancelled by user — no automatic restart. "
                f"Task state saved:\n{self.autofix_task.file_path() if self.autofix_task else '(n/a)'}",
            )
        else:
            self._status("AutoFix: Failed")
            self.pipeline_finished.emit(False, f"Execution FAILED.\n{detail}")

    # ------------------------------------------------------------------
    # Memory helpers
    # ------------------------------------------------------------------

    def _record_session(self, outcome: str):
        try:
            record_memory(
                self._workspace,
                KIND_SESSIONS,
                f"session-{self.autofix_task.task_id}" if self.autofix_task else "session",
                (
                    f"AutoFix pipeline session outcome: {outcome}\n"
                    f"Backend: {self.backend_used or 'n/a'}\n"
                    f"Recovery attempts: {self.autofix_task.recovery_attempts if self.autofix_task else 0}"
                ),
                tags=["autofix"],
                session_id=(
                    self.autofix_task.opencode_session if self.autofix_task else None
                ),
            )
        except OSError:
            pass

    def _record_error(self, task_obj: AutoFixTask, error: str, attempt: int):
        try:
            record_memory(
                self._workspace,
                KIND_ERRORS,
                f"error-{task_obj.task_id}-attempt{attempt}",
                f"{error}\n\nLast output tail:\n{task_obj.output_tail(20)}",
                tags=["autofix", task_obj.task_id],
            )
        except OSError:
            pass

    def _finish(self, success: bool, message: str):
        if not self._cancelled:
            self.pipeline_finished.emit(success, message)
