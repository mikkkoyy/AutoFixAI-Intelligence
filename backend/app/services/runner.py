from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
import sys

from app.security import validate_command


@dataclass
class RunResult:
    passed: bool
    return_code: int
    stdout: str
    stderr: str


class TestRunner:
    def _clear_python_caches(self, workspace: Path) -> None:
        """Remove Python bytecode caches before each isolated test run.

        This is important on filesystems with coarse timestamp resolution:
        a fixer can rewrite a same-size .py file within the same timestamp
        tick, causing Python to reuse stale bytecode from the previous run.
        """
        for cache in workspace.rglob("__pycache__"):
            if cache.is_dir():
                shutil.rmtree(cache, ignore_errors=True)

        for pyc in workspace.rglob("*.pyc"):
            if pyc.is_file():
                try:
                    pyc.unlink()
                except OSError:
                    pass

    def _resolve_command(self, command: list[str]) -> list[str]:
        """Transform the command to use the current Python interpreter.

        When the command starts with ``pytest``, rewrite it to
        ``sys.executable -m pytest …`` so that the subprocess uses the
        same interpreter (and virtual-environment) that is running the
        backend, rather than relying on ``pytest`` being on the system
        PATH.
        """
        if command and Path(command[0]).stem.lower() == "pytest":
            return [sys.executable, "-m", "pytest"] + command[1:]
        return command

    def run(self, workspace: Path, command: list[str]) -> RunResult:
        validate_command(command)
        self._clear_python_caches(workspace)

        resolved = self._resolve_command(command)

        env = os.environ.copy()
        # Do not create new bytecode in an AutoFix test workspace.
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        try:
            p = subprocess.run(
                resolved,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=180,
                shell=False,
                env=env,
            )
            return RunResult(
                passed=p.returncode == 0,
                return_code=p.returncode,
                stdout=p.stdout,
                stderr=p.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                False,
                124,
                exc.stdout or "",
                "Test execution timed out.",
            )
