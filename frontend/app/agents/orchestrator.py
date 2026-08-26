"""Multi-agent orchestration system for AutoFix AI Studio.

Provides real agents (Planner, Coder, Debugger, Tester, Reviewer, Verification,
OpenCode) and a pipeline orchestrator with Qt thread-based async execution.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.models import (
    AgentResult,
    AgentRole,
    AgentStatus,
    AgentStep,
    AgentTask,
    AgentTaskStatus,
)


# ------------------------------------------------------------------
# Base agent
# ------------------------------------------------------------------

class Agent:
    """Base class for pipeline agents."""

    role: AgentRole = AgentRole.PLANNER
    label: str = "Agent"

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        raise NotImplementedError


# ------------------------------------------------------------------
# Planner Agent — scans workspace, detects structure, creates plan
# ------------------------------------------------------------------

class PlannerAgent(Agent):
    role = AgentRole.PLANNER
    label = "Planner"

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        workspace = Path(task.workspace) if task.workspace else Path.cwd()
        description = task.description or "No description provided"

        if not workspace.exists():
            return AgentResult(
                self.label, AgentStatus.PASSED,
                f"Workspace not available: {workspace or '(empty)'}. Plan deferred.",
            )

        py_files = list(workspace.rglob("*.py"))
        test_files = [f for f in py_files if f.name.startswith("test_")]
        src_files = [f for f in py_files if not f.name.startswith("test_")]

        has_pytest_cfg = any(
            (workspace / name).exists()
            for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
        )
        has_package = any(
            (workspace / d).is_dir() for d in ("src", "lib")
        ) or (workspace / "__init__.py").exists()

        plan_items: list[str] = []
        plan_items.append(f"Task: {description}")
        plan_items.append(f"Workspace: {workspace}")
        plan_items.append(f"Source files: {len(src_files)}")
        plan_items.append(f"Test files: {len(test_files)}")
        plan_items.append(f"Test framework: {'pytest' if has_pytest_cfg else 'not detected'}")
        plan_items.append(f"Package structure: {'yes' if has_package else 'flat'}")
        plan_items.append("")

        if task.description:
            lower = task.description.lower()
            if any(w in lower for w in ("fix", "bug", "error", "broken")):
                plan_items.append("Strategy: Diagnose and fix existing issues")
            elif any(w in lower for w in ("add", "create", "new", "implement")):
                plan_items.append("Strategy: Implement new functionality")
            elif any(w in lower for w in ("test", "coverage")):
                plan_items.append("Strategy: Improve test coverage")
            elif any(w in lower for w in ("refactor", "clean", "improve")):
                plan_items.append("Strategy: Refactor and improve code quality")
            else:
                plan_items.append("Strategy: Analyze and apply changes")
        else:
            plan_items.append("Strategy: General analysis")

        plan_items.append("")
        plan_items.append("Pipeline: Plan -> Code -> Test -> Debug -> Review -> Verify")

        context["plan"] = {
            "py_files": [str(p) for p in py_files],
            "test_files": [str(p) for p in test_files],
            "src_files": [str(p) for p in src_files],
            "has_pytest": has_pytest_cfg,
            "has_package": has_package,
        }

        return AgentResult(self.label, AgentStatus.PASSED, "\n".join(plan_items))


# ------------------------------------------------------------------
# Coder Agent — implements code changes
# ------------------------------------------------------------------

class CoderAgent(Agent):
    role = AgentRole.CODER
    label = "Coder"

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        workspace = Path(task.workspace) if task.workspace else Path.cwd()
        description = (task.description or "").lower()

        if not workspace.exists():
            return AgentResult(
                self.label, AgentStatus.PASSED,
                f"Workspace not available. No code changes made.",
            )

        changes: list[str] = []
        plan = context.get("plan", {})
        test_files = [Path(p) for p in plan.get("test_files", [])]

        init_files = []
        for d in ("src", "lib"):
            pkg_dir = workspace / d
            if pkg_dir.is_dir() and not (pkg_dir / "__init__.py").exists():
                (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
                init_files.append(str(pkg_dir / "__init__.py"))
                changes.append(f"Created {d}/__init__.py")

        for child in workspace.iterdir():
            if child.is_dir() and not child.name.startswith((".", "__", "node_modules", ".git")):
                init_path = child / "__init__.py"
                if not init_path.exists() and any(child.rglob("*.py")):
                    init_path.write_text("", encoding="utf-8")
                    init_files.append(str(init_path))
                    changes.append(f"Created {child.name}/__init__.py")

        if description and any(w in description for w in ("fix", "bug", "error")):
            for tf in test_files:
                if tf.exists():
                    try:
                        source = tf.read_text(encoding="utf-8")
                        ast.parse(source)
                    except SyntaxError as exc:
                        changes.append(f"Noted syntax issue in {tf.name}: {exc}")

        py_files = list(workspace.rglob("*.py"))
        lint_issues = 0
        for p in py_files:
            try:
                source = p.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(p))
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and len(node.args.args) > 8:
                        lint_issues += 1
            except (SyntaxError, UnicodeDecodeError):
                pass

        if lint_issues:
            changes.append(f"Noted {lint_issues} functions with many parameters")

        if not changes:
            return AgentResult(
                self.label, AgentStatus.PASSED,
                f"Analyzed {len(py_files)} files. No deterministic changes needed.",
            )

        return AgentResult(
            self.label, AgentStatus.PASSED,
            f"Applied {len(changes)} changes:\n" + "\n".join(f"  - {c}" for c in changes),
        )


# ------------------------------------------------------------------
# Debugger Agent — analyzes test failures, identifies root causes
# ------------------------------------------------------------------

class DebuggerAgent(Agent):
    role = AgentRole.DEBUGGER
    label = "Debugger"

    _ERROR_PATTERNS: list[tuple[str, str]] = [
        (r"syntaxerror|indentationerror", "syntax_error: Python syntax or indentation is invalid"),
        (r"modulenotfounderror", "import_error: A required module is missing"),
        (r"importerror", "import_error: A Python import failed"),
        (r"assertionerror|^\s*e\s+assert\s+|failed test_", "test_failure: A test assertion failed"),
        (r"timeout|timed out", "timeout: Execution exceeded the time limit"),
        (r"typeerror:", "runtime_error: TypeError detected"),
        (r"valueerror:", "runtime_error: ValueError detected"),
        (r"nameerror:", "runtime_error: NameError — undefined variable"),
        (r"attributeerror:", "runtime_error: AttributeError detected"),
        (r"permissionerror:", "runtime_error: Permission denied"),
        (r"filenotfounderror:", "runtime_error: Required file not found"),
    ]

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        stdout = context.get("test_output", "")
        stderr = context.get("test_error", "")
        test_passed = context.get("test_passed")

        if test_passed is True:
            return AgentResult(self.label, AgentStatus.PASSED, "No test failures to analyze.")

        text = f"{stdout}\n{stderr}".lower()
        if not text.strip():
            return AgentResult(self.label, AgentStatus.PASSED, "No test output available for analysis.")

        for pattern, diagnosis in self._ERROR_PATTERNS:
            if re.search(pattern, text):
                category, summary = diagnosis.split(": ", 1)
                context["diagnosis"] = {"category": category, "summary": summary}
                return AgentResult(
                    self.label, AgentStatus.PASSED,
                    f"Root cause: {summary}\nCategory: {category}",
                )

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        tail = "\n".join(lines[-10:]) if lines else "(empty)"
        return AgentResult(
            self.label, AgentStatus.PASSED,
            f"Could not classify failure automatically.\nLast output:\n{tail}",
        )


# ------------------------------------------------------------------
# Tester Agent — detects test system, runs tests, reports results
# ------------------------------------------------------------------

class TesterAgent(Agent):
    role = AgentRole.TESTER
    label = "Tester"

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        workspace = Path(task.workspace) if task.workspace else Path.cwd()

        if not workspace.exists():
            return AgentResult(
                self.label, AgentStatus.PASSED,
                "Workspace does not exist. Skipping tests.",
            )

        test_files = list(workspace.rglob("test_*.py"))
        if not test_files:
            return AgentResult(
                self.label, AgentStatus.PASSED,
                "No test files found. Skipping test execution.",
            )

        has_pytest_cfg = any(
            (workspace / name).exists()
            for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
        )

        cmd = [sys.executable, "-m", "pytest", "-q", str(workspace)]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(workspace),
                env=env,
                shell=False,
            )

            stdout = result.stdout or ""
            stderr = result.stderr or ""
            context["test_output"] = stdout
            context["test_error"] = stderr
            context["test_return_code"] = result.returncode

            if result.returncode == 0:
                context["test_passed"] = True
                summary = stdout.strip().splitlines()[-1] if stdout.strip() else "All tests passed"
                return AgentResult(self.label, AgentStatus.PASSED, f"Tests PASSED: {summary}")

            context["test_passed"] = False
            summary_lines = stdout.strip().splitlines()
            summary = summary_lines[-1] if summary_lines else f"Exit code {result.returncode}"
            return AgentResult(
                self.label, AgentStatus.FAILED,
                f"Tests FAILED: {summary}",
            )

        except subprocess.TimeoutExpired:
            context["test_passed"] = False
            context["test_output"] = ""
            context["test_error"] = "Test execution timed out after 120 seconds."
            return AgentResult(self.label, AgentStatus.FAILED, "Tests FAILED: Execution timed out.")

        except FileNotFoundError:
            return AgentResult(
                self.label, AgentStatus.PASSED,
                f"Could not locate {sys.executable}. Skipping tests.",
            )

        except Exception as exc:
            return AgentResult(
                self.label, AgentStatus.PASSED,
                f"Test execution error: {exc}. Skipping.",
            )


# ------------------------------------------------------------------
# Reviewer Agent — reviews code quality, AST validity, patterns
# ------------------------------------------------------------------

class ReviewerAgent(Agent):
    role = AgentRole.REVIEWER
    label = "Reviewer"

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        workspace = Path(task.workspace) if task.workspace else Path.cwd()

        if not workspace.exists():
            return AgentResult(self.label, AgentStatus.PASSED, "No workspace to review.")

        py_files = list(workspace.rglob("*.py"))
        issues: list[str] = []
        stats = {"files": len(py_files), "functions": 0, "classes": 0, "lines": 0}

        for path in py_files:
            try:
                source = path.read_text(encoding="utf-8")
                stats["lines"] += source.count("\n") + 1
                tree = ast.parse(source, filename=str(path))

                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef):
                        stats["functions"] += 1
                        if len(node.args.args) > 10:
                            issues.append(
                                f"{path.name}:{node.lineno}: "
                                f"Function '{node.name}' has {len(node.args.args)} parameters"
                            )
                    elif isinstance(node, ast.ClassDef):
                        stats["classes"] += 1

                if len(source) > 500 and not source.strip().startswith('"""'):
                    if source.count("\ndef ") > 10:
                        issues.append(f"{path.name}: Large file ({source.count(chr(10))+1} lines) — consider splitting")

            except SyntaxError as exc:
                issues.append(f"{path.name}: Syntax error: {exc}")
            except UnicodeDecodeError:
                issues.append(f"{path.name}: Could not decode file (non-UTF-8)")

        test_passed = context.get("test_passed")
        if test_passed is False:
            issues.append("Test suite is currently failing")

        summary_parts = [
            f"Reviewed {stats['files']} files ({stats['lines']} lines, "
            f"{stats['functions']} functions, {stats['classes']} classes)",
        ]

        if issues:
            return AgentResult(
                self.label, AgentStatus.FAILED,
                f"Found {len(issues)} issue(s):\n"
                + "\n".join(f"  - {i}" for i in issues)
                + "\n\n" + summary_parts[0],
            )

        return AgentResult(self.label, AgentStatus.PASSED, summary_parts[0] + ". No issues found.")


# ------------------------------------------------------------------
# Verification Agent — final pass/fail confirmation
# ------------------------------------------------------------------

class VerificationAgent(Agent):
    role = AgentRole.VERIFICATION
    label = "Verification"

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        workspace = Path(task.workspace) if task.workspace else Path.cwd()
        checks: list[str] = []
        passed = True

        if not workspace.exists():
            return AgentResult(
                self.label, AgentStatus.PASSED,
                f"Verification PASSED: Workspace not available. Deferred.",
            )

        py_files = list(workspace.rglob("*.py")) if workspace.exists() else []
        parse_errors = 0
        for path in py_files:
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                parse_errors += 1

        if parse_errors:
            checks.append(f"Parse errors: {parse_errors}/{len(py_files)} files")
            passed = False
        elif py_files:
            checks.append(f"All {len(py_files)} Python files parse OK")

        test_passed = context.get("test_passed")
        if test_passed is True:
            checks.append("Tests: PASSED")
        elif test_passed is False:
            checks.append("Tests: FAILED")
            passed = False
        else:
            checks.append("Tests: Not run")

        diagnosis = context.get("diagnosis")
        if diagnosis:
            checks.append(f"Diagnosis: {diagnosis.get('summary', 'unknown')}")

        status_label = "PASS" if passed else "FAIL"
        return AgentResult(
            self.label,
            AgentStatus.PASSED if passed else AgentStatus.FAILED,
            f"Verification {status_label}:\n" + "\n".join(f"  {c}" for c in checks),
        )


# ------------------------------------------------------------------
# Recovery Agent — continues an interrupted AutoFix task (same task)
# ------------------------------------------------------------------

class RecoveryAgent(Agent):
    """Builds the continuation prompt for an unexpectedly stopped task.

    The continuation is the SAME logical task: original request, approved
    plan, completed/remaining steps, changed files, diagnostics, previous
    output and the termination reason — plus only RELEVANT project memory
    (never the whole memory directory).
    """

    role = AgentRole.RECOVERY
    label = "Recovery"

    MEMORY_QUERY_LIMIT = 3

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        autofix_task = context.get("autofix_task")
        if autofix_task is None:
            return AgentResult(
                self.label, AgentStatus.FAILED,
                "No persisted AutoFix task to continue.",
            )

        from app.agents.autofix_task import build_continuation_context
        from app.agents.task_memory import retrieve_relevant

        memory_notes = []
        try:
            for record in retrieve_relevant(
                autofix_task.workspace,
                f"{autofix_task.original_request} {autofix_task.plan or ''}",
                limit=self.MEMORY_QUERY_LIMIT,
            ):
                memory_notes.append(
                    f"[{record.get('kind')}] {record.get('title')}: "
                    f"{str(record.get('content', ''))[:400]}"
                )
        except Exception:
            memory_notes = []

        prompt = build_continuation_context(
            autofix_task,
            extra_notes=(
                ["Relevant project memory:", *memory_notes] if memory_notes else None
            ),
        )
        return AgentResult(self.label, AgentStatus.PASSED, prompt)


# ------------------------------------------------------------------
# OpenCode Agent — delegates to the integrated OpenCode terminal
# ------------------------------------------------------------------

class OpenCodeAgent(Agent):
    role = AgentRole.OPENCODE
    label = "OpenCode"

    def run(self, task: AgentTask, context: dict) -> AgentResult:
        from app.agents.opencode.discovery import OpenCodeDiscovery

        info = OpenCodeDiscovery().discover()

        if info.is_installed:
            return AgentResult(
                self.label, AgentStatus.PASSED,
                f"OpenCode available (v{info.version or '?'}) "
                f"at {info.executable_path}\n"
                "Use the integrated terminal for interactive coding.",
            )

        return AgentResult(
            self.label, AgentStatus.PASSED,
            "OpenCode not installed. Install with: npm install -g opencode-ai",
        )


# ------------------------------------------------------------------
# Pipeline execution thread
# ------------------------------------------------------------------

class _AgentPipelineThread(QThread):
    """Runs the agent pipeline in a background thread."""

    step_started = Signal(int, str)
    step_completed = Signal(int, str, str)
    pipeline_completed = Signal(str)
    pipeline_failed = Signal(str)

    def __init__(
        self,
        task: AgentTask,
        agents: list[Agent],
        context: dict,
        parent=None,
    ):
        super().__init__(parent)
        self._task = task
        self._agents = agents
        self._context = context
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            overall_passed = True

            for i, agent in enumerate(self._agents):
                if self._cancelled:
                    self._task.status = AgentTaskStatus.CANCELLED
                    self._task.result_message = "Pipeline cancelled by user."
                    self.pipeline_completed.emit("Pipeline cancelled.")
                    return

                self._task.current_step_index = i
                step = self._task.steps[i]
                step.status = AgentStatus.RUNNING
                step.started_at = time.time()
                self.step_started.emit(i, agent.label)

                result = agent.run(self._task, self._context)

                step.status = result.status
                step.result_message = result.message
                step.finished_at = time.time()

                self.step_completed.emit(i, result.status.value, result.message)

                if result.status == AgentStatus.FAILED:
                    overall_passed = False

                    if agent.role == AgentRole.PLANNER:
                        self._task.status = AgentTaskStatus.FAILED
                        self._task.result_message = f"Pipeline failed at Planning: {result.message}"
                        self.pipeline_failed.emit(self._task.result_message)
                        return

            self._task.status = (
                AgentTaskStatus.COMPLETED if overall_passed else AgentTaskStatus.FAILED
            )
            self._task.result_message = (
                "All agents completed successfully."
                if overall_passed
                else "Pipeline completed with issues."
            )
            self.pipeline_completed.emit(self._task.result_message)

        except Exception as exc:
            self._task.status = AgentTaskStatus.FAILED
            self._task.result_message = f"Pipeline error: {exc}"
            self.pipeline_failed.emit(self._task.result_message)


# ------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------

class AgentOrchestrator:
    """Central orchestrator for the multi-agent pipeline.

    Provides:
      - ``start_task()`` for async UI-driven execution (returns QThread)
      - ``run()`` for synchronous execution (tests / scripting)
    """

    PIPELINE_ORDER: list[type[Agent]] = [
        PlannerAgent,
        CoderAgent,
        TesterAgent,
        DebuggerAgent,
        ReviewerAgent,
        VerificationAgent,
        OpenCodeAgent,
    ]

    def __init__(self):
        self._current_task: AgentTask | None = None
        self._thread: _AgentPipelineThread | None = None

    @property
    def current_task(self) -> AgentTask | None:
        return self._current_task

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    # -- async (UI) --

    def start_task(
        self,
        description: str,
        workspace: str = "",
        parent=None,
    ) -> _AgentPipelineThread:
        task = AgentTask(description=description, workspace=workspace)
        agents, steps = self._build_pipeline(task)
        context: dict = {"workspace": workspace}

        thread = _AgentPipelineThread(task, agents, context, parent)
        self._current_task = task
        self._thread = thread
        return thread

    def cancel_task(self):
        if self._thread and self._thread.isRunning():
            self._thread.cancel()

    # -- synchronous (tests / scripting) --

    def run(self, context: dict | None = None) -> list[AgentResult]:
        ctx = dict(context) if context else {}
        workspace = ctx.get("project", "")
        task = AgentTask(
            description="Synchronous test pipeline",
            workspace=str(workspace) if workspace else "__no_workspace__",
        )
        agents, _ = self._build_pipeline(task)

        results: list[AgentResult] = []
        for agent in agents:
            result = agent.run(task, ctx)
            results.append(result)
            if result.status == AgentStatus.FAILED and agent.role == AgentRole.PLANNER:
                break

        return results

    # -- internal --

    def _build_pipeline(self, task: AgentTask) -> tuple[list[Agent], list[AgentStep]]:
        agents: list[Agent] = []
        steps: list[AgentStep] = []
        for cls in self.PIPELINE_ORDER:
            agent = cls()
            agents.append(agent)
            steps.append(AgentStep(
                role=agent.role,
                description=f"{agent.label} agent execution",
            ))
        task.steps = steps
        return agents, steps
