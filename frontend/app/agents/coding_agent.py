"""Coding-agent execution layer for AutoFix AI Studio.

Priority (first available backend wins):
    1. OpenCode  — ``opencode run "<prompt>"`` in the workspace.
    2. OpenHands — ``openhands --headless -t "<prompt>"`` in the workspace.
    3. Continue  — ``cn -p "<prompt>"`` in the workspace.
    4. Aider     — ``aider --yes-always --message "<prompt>"`` in the workspace.

Large prompts are never placed on the command line: they are persisted under
``<workspace>\\.autofix\\tasks\\`` and the CLI receives a compact bootstrap
instruction pointing at the payload file (see app.agents.task_transport).

All backends operate against the currently active project workspace.  If a
backend cannot start, the next one is tried automatically.  If no backend is
available the caller receives an honest failure result — execution is never
fabricated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.task_transport import TransportPlan, prepare_task_payload

DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AUTOFIX_AGENT_TIMEOUT", "900"))

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Automatic priority chain — first available backend becomes primary.
PRIORITY_ORDER = ("opencode", "openhands", "continue", "aider")


@dataclass
class BackendInfo:
    """Detection result for a single coding backend."""

    name: str
    available: bool
    executable: str | None = None
    detail: str = ""


@dataclass
class CodingResult:
    """Outcome of a coding-agent execution attempt."""

    backend: str | None
    success: bool
    output: str = ""
    error: str = ""
    timed_out: bool = False
    #: False when the process could not be launched at all (the caller may
    #: then fall back to the next backend in the priority chain).
    started: bool = True
    worker_name: str | None = None
    fallback_reason: str | None = None

    @property
    def summary(self) -> str:
        if self.success:
            return f"Coding agent '{self.backend}' completed successfully."
        if self.backend is None:
            return self.error or "No coding agent available."
        if self.timed_out:
            return f"Coding agent '{self.backend}' timed out."
        if not self.started:
            return f"Coding agent '{self.backend}' could not be started."
        return f"Coding agent '{self.backend}' failed: {self.error or 'unknown error'}"


def _default_discover_opencode() -> BackendInfo:
    try:
        from app.agents.opencode.discovery import OpenCodeDiscovery

        info = OpenCodeDiscovery().discover()
        if info.is_installed:
            executable = _resolve_windows_executable(info.executable_path or "opencode")
            return BackendInfo(
                "opencode", True, executable,
                detail=f"version {info.version}" if info.version else "",
            )
        return BackendInfo("opencode", False, None, info.error or "not found on PATH")
    except Exception as exc:  # pragma: no cover - defensive
        return BackendInfo("opencode", False, None, str(exc))


def _default_discover_aider() -> BackendInfo:
    path = shutil.which("aider") or shutil.which("aider.exe") or shutil.which("aider.cmd")
    if path:
        return BackendInfo("aider", True, path)
    return BackendInfo("aider", False, None, "aider not found on PATH")


def _default_discover_openhands() -> BackendInfo:
    """Detect the OpenHands CLI (``openhands``).

    Only a real executable counts as available — an interactive-only install
    without the CLI is reported unavailable rather than pretended to work.
    """
    for name in ("openhands", "openhands.exe", "openhands.cmd", "openhands.bat"):
        path = shutil.which(name)
        if path:
            return BackendInfo("openhands", True, path, detail="CLI on PATH")
    return BackendInfo("openhands", False, None, "openhands CLI not found on PATH")


def _default_discover_continue() -> BackendInfo:
    """Detect the Continue CLI (``cn``).

    A ``~/.continue`` configuration directory alone does NOT make Continue
    usable — only an actual CLI binary on PATH marks it available.
    """
    for name in ("cn", "cn.exe", "cn.cmd", "cn.bat"):
        path = shutil.which(name)
        if path:
            return BackendInfo("continue", True, path, detail="CLI on PATH")
    if Path.home().joinpath(".continue").is_dir():
        return BackendInfo(
            "continue",
            False,
            None,
            "~/.continue config found but no cn CLI on PATH",
        )
    return BackendInfo("continue", False, None, "Continue CLI (cn) not found on PATH")


def _resolve_windows_executable(path: str) -> str:
    """Prefer a Windows-launchable shim (.cmd/.exe) over a POSIX sh script."""
    candidate = Path(path)
    suffixes = {".cmd", ".com", ".exe", ".bat"}
    if candidate.suffix.lower() in suffixes or os.name != "nt":
        return str(candidate)
    for ext in (".cmd", ".exe", ".bat"):
        sibling = candidate.with_suffix(ext)
        if sibling.exists():
            return str(sibling)
    return str(candidate)


class CodingAgentRunner:
    """Executes approved coding tasks via the automatic priority chain
    OpenCode → OpenHands → Continue → Aider.

    Args:
        discover_opencode: Injectable discovery callable (tests).
        discover_openhands: Injectable discovery callable (tests).
        discover_continue: Injectable discovery callable (tests).
        discover_aider: Injectable discovery callable (tests).
        popen: Injectable subprocess.Popen factory (tests).
    """

    def __init__(
        self,
        discover_opencode=None,
        discover_aider=None,
        popen=None,
        discover_openhands=None,
        discover_continue=None,
    ):
        self._discover_opencode = discover_opencode or _default_discover_opencode
        self._discover_openhands = discover_openhands or _default_discover_openhands
        self._discover_continue = discover_continue or _default_discover_continue
        self._discover_aider = discover_aider or _default_discover_aider
        self._popen = popen or subprocess.Popen
        #: Transport plan of the most recent execute() call (introspection).
        self.last_transport: TransportPlan | None = None
        #: One-shot extra CLI arguments consumed by the NEXT opencode
        #: invocation (e.g. ["--session", <id>] after capability probing).
        self.pending_extra_args: list[str] | None = None
        self._active_process = None

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_backends(self) -> dict[str, BackendInfo]:
        return {
            "opencode": self._discover_opencode(),
            "openhands": self._discover_openhands(),
            "continue": self._discover_continue(),
            "aider": self._discover_aider(),
        }

    def primary_backend(self) -> BackendInfo:
        """Return the highest-priority available backend."""
        backends = self.detect_backends()
        for name in PRIORITY_ORDER:
            info = backends[name]
            if info.available and info.executable:
                return info
        return BackendInfo("none", False, None, "No coding agent available.")

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        prompt: str,
        workspace: str | Path,
        on_output=None,
        timeout: int | None = None,
    ) -> CodingResult:
        """Run *prompt* against *workspace* using the priority chain.

        A backend that is unavailable — or that cannot even be started —
        is skipped automatically in favour of the next one.  A backend that
        starts but fails honestly is reported as-is (no silent fallback that
        could mask a real task failure).
        """
        timeout = timeout or DEFAULT_TIMEOUT_SECONDS
        workspace = str(workspace)

        # Large tasks are NEVER passed as one giant command-line argument.
        # The complete prompt is persisted under <workspace>\.autofix\tasks\
        # and the CLI receives a compact bootstrap instruction instead.
        payload_context = getattr(self, "payload_context", None)
        self.payload_context = None
        self.last_transport = prepare_task_payload(
            prompt, workspace, extra_context=payload_context
        )
        effective_prompt = self.last_transport.command_prompt

        backends = self.detect_backends()
        first_failure: CodingResult | None = None

        for name in PRIORITY_ORDER:
            info = backends[name]
            if not (info.available and info.executable):
                continue

            command = self._build_command(name, info.executable, effective_prompt)
            result = self._run_command(
                name, command, workspace, on_output, timeout
            )
            if result.success or result.started:
                return result

            # Could not start at all — remember and try the next backend.
            if first_failure is None:
                first_failure = result

        if first_failure is not None:
            return first_failure

        return CodingResult(
            backend=None,
            success=False,
            error=(
                "No coding agent available. Install OpenCode "
                "(npm install -g opencode-ai), OpenHands "
                "(uv tool install openhands), Continue "
                "(npm i -g @continuedev/cli) or Aider (pip install aider-chat)."
            ),
        )

    def _build_command(self, backend: str, executable: str, prompt: str) -> list[str]:
        """Non-interactive invocation for each supported backend."""
        if backend == "opencode":
            extra = list(self.pending_extra_args or [])
            self.pending_extra_args = None  # consume exactly once
            return [executable, "run", *extra, prompt]
        if backend == "openhands":
            # Documented headless mode: openhands --headless -t "<task>"
            return [executable, "--headless", "-t", prompt]
        if backend == "continue":
            # Documented headless mode: cn -p "<prompt>"
            return [executable, "-p", prompt]
        if backend == "aider":
            return self._build_aider_command(executable, prompt)
        raise ValueError(f"Unknown coding backend: {backend}")

    def _build_aider_command(self, executable: str, prompt: str) -> list[str]:
        command = [executable, "--yes-always", "--message", prompt]

        model = os.environ.get("AUTOFIX_AIDER_MODEL", "").strip()
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

        if model:
            if openrouter_key and not model.startswith("openrouter/"):
                model = f"openrouter/{model}"
            command.extend(["--model", model])

        return command

    def cancel_active(self):
        """Kill the running coding-agent process (user cancellation)."""
        process = self._active_process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            pass

    def _run_command(
        self,
        backend: str,
        command: list[str],
        workspace: str,
        on_output,
        timeout: int,
    ) -> CodingResult:
        env = os.environ.copy()
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

        collected: list[str] = []

        try:
            process = self._popen(
                command,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            return CodingResult(
                backend=backend,
                success=False,
                error=f"Failed to start {backend}: {exc}",
                started=False,
            )
        self._active_process = process

        timed_out = False
        try:
            for line in process.stdout or []:
                text = line.rstrip("\r\n")
                if not text:
                    continue
                collected.append(text)
                if on_output:
                    try:
                        on_output(text)
                    except Exception:
                        pass
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                process.wait(timeout=5)
            except Exception:
                pass

        output = "\n".join(collected)
        self._active_process = None

        if timed_out:
            return CodingResult(
                backend=backend, success=False, output=output,
                error=f"{backend} exceeded {timeout}s timeout.", timed_out=True,
            )

        if process.returncode == 0:
            return CodingResult(backend=backend, success=True, output=output)

        return CodingResult(
            backend=backend,
            success=False,
            output=output,
            error=self._describe_failure(backend, process.returncode, output),
        )

    def _describe_failure(self, backend: str, returncode: int, output: str) -> str:
        lowered = output.lower()

        if "unknown model" in lowered or "invalid model" in lowered or "no such model" in lowered:
            return (
                f"{backend} rejected the configured model. Check AUTOFIX_AIDER_MODEL "
                f"(exit code {returncode})."
            )
        if "api key" in lowered or "unauthorized" in lowered or "401" in lowered:
            return (
                f"{backend} reported an authentication/model-configuration problem "
                f"(exit code {returncode}). Verify provider API keys."
            )
        if "not recognized" in lowered or "no such file" in lowered:
            return f"{backend} executable could not be launched (exit code {returncode})."

        tail = "\n".join(output.splitlines()[-8:]) if output else ""
        return f"{backend} exited with code {returncode}." + (f"\n{tail}" if tail else "")
