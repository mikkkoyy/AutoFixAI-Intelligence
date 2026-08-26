from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from app.agents.coding_agent import CodingResult


@dataclass
class CopilotWorker:
    """Internal GitHub Copilot coding worker.

    The worker is optional and must remain internal. If the CLI is not present,
    not authenticated, or cannot start, the router continues to the next worker.
    """

    executable: str | None = None

    def __post_init__(self):
        if self.executable is None:
            candidates = [
                "copilot",
                "github-copilot",
                "gh",
                "gh.exe",
            ]
            for name in candidates:
                found = shutil.which(name)
                if found:
                    self.executable = found
                    break

    def is_available(self) -> bool:
        return bool(self.executable)

    def execute(self, prompt: str, workspace: str, on_output=None, timeout: int | None = None) -> CodingResult:
        if not self.is_available():
            return CodingResult(
                backend="copilot",
                success=False,
                error="Copilot unavailable",
                started=False,
            )

        cmd = [self.executable, "suggest", prompt]
        effective_timeout = timeout or 300
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            # The process started but exceeded its budget — a TIMEOUT, not an
            # unavailable worker. The router persists this and may fall back.
            return CodingResult(
                backend="copilot",
                success=False,
                error=f"copilot exceeded {effective_timeout}s timeout.",
                timed_out=True,
            )
        except (FileNotFoundError, OSError) as exc:
            return CodingResult(
                backend="copilot",
                success=False,
                error=f"Copilot start failed: {exc}",
                started=False,
            )

        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            if on_output is not None:
                on_output(output)
            return CodingResult(backend="copilot", success=True, output=output, started=True, worker_name="copilot")
        return CodingResult(
            backend="copilot",
            success=False,
            error=output or "Copilot failed",
            started=True,
        )
