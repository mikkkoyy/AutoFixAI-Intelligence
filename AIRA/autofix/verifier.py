"""Run the smallest targeted test, then the full pytest suite."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from AIRA.autofix.models import VerificationResult
from AIRA.core.logging import get_logger

logger = get_logger("autofix")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_PYTEST_NODE_RE = re.compile(r"^([^\s]+\.py)(::[^:\s]+)?$")

RunCommand = Callable[..., subprocess.CompletedProcess]


def _default_run_command(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


class Verifier:
    """Executes targeted tests and the full test suite, capturing results."""

    def __init__(
        self,
        repo_root: Path = PROJECT_ROOT,
        python: Optional[str] = None,
        timeout: int = 600,
        run_command: Optional[RunCommand] = None,
    ):
        self.repo_root = Path(repo_root)
        self.python = python or sys.executable
        self.timeout = timeout
        self.run_command = run_command or _default_run_command

    def run(self, cmd: list[str]) -> tuple[int, str, str, float]:
        started = time.monotonic()
        try:
            proc = self.run_command(cmd, cwd=self.repo_root, timeout=self.timeout)
            duration = time.monotonic() - started
            return (proc.returncode, proc.stdout or "", proc.stderr or "", duration)
        except subprocess.TimeoutExpired as e:
            duration = time.monotonic() - started
            return (1, e.stdout or "", f"Command timed out after {self.timeout}s", duration)
        except FileNotFoundError as e:
            duration = time.monotonic() - started
            return (1, "", f"Command not found: {e}", duration)

    def _command_for(self, test_name: Optional[str]) -> list[str]:
        return [self.python, "-m", "pytest", test_name, "-q"]

    def run_targeted_test(self, test_name: Optional[str]) -> VerificationResult:
        result = VerificationResult(targeted_test=test_name)
        if not test_name:
            result.stderr = "No targeted test was provided in safe mode."
            return result

        target = test_name.strip()
        if " " in target and not _PYTEST_NODE_RE.match(target):
            target = target.split()[0]

        rc, stdout, stderr, duration = self.run(self._command_for(target))
        result.targeted_test = target
        result.targeted_passed = rc == 0
        result.stdout = (stdout or "")[:8000]
        result.stderr = (stderr or "")[:8000]
        result.duration = duration
        return result

    def run_full_suite(self) -> tuple[bool, str, str, float]:
        rc, stdout, stderr, duration = self.run(
            [self.python, "-m", "pytest", "tests", "-q"]
        )
        return rc == 0, stdout, stderr, duration

    def verify(self, test_name: Optional[str] = None) -> VerificationResult:
        """Run the targeted test, then the full suite. Both must pass."""
        targeted = self.run_targeted_test(test_name)
        full_ok, full_out, full_err, full_duration = self.run_full_suite()

        combined_stdout = targeted.stdout
        combined_stderr = targeted.stderr
        if full_out:
            combined_stdout += "\n--- full suite ---\n" + full_out
        if full_err:
            combined_stderr += "\n--- full suite stderr ---\n" + full_err

        return VerificationResult(
            targeted_test=targeted.targeted_test,
            targeted_passed=targeted.targeted_passed,
            full_suite_passed=full_ok,
            stdout=combined_stdout[:16000],
            stderr=combined_stderr[:16000],
            duration=targeted.duration + full_duration,
        )