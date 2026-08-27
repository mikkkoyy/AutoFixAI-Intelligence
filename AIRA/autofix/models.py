"""Shared data models for the AIRA AutoFix engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from AIRA.core.models import timestamp_now


@dataclass
class ErrorReport:
    """Normalized description of a failure detected by the error monitor."""

    error_type: str
    message: str
    traceback: str = ""
    source_file: Optional[str] = None
    source_line: Optional[int] = None
    test_name: Optional[str] = None
    timestamp: str = field(default_factory=timestamp_now)
    command: Optional[str] = None
    repository_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "traceback": self.traceback,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "test_name": self.test_name,
            "timestamp": self.timestamp,
            "command": self.command,
            "repository_path": self.repository_path,
        }

    @property
    def error_signature(self) -> str:
        base = f"{self.error_type}: {self.message[:120]}".strip()
        if self.source_file:
            base += f" @ {self.source_file}"
            if self.source_line:
                base += f":{self.source_line}"
        return base


VALID_RISK_LEVELS = ("low", "medium", "high")

ANALYSIS_SCHEMA_FIELDS = (
    "root_cause",
    "confidence",
    "affected_files",
    "fix_strategy",
    "patch",
    "targeted_tests",
    "risk",
)


@dataclass
class FixProposal:
    """Validated AI analysis that proposes a fix."""

    root_cause: str
    confidence: float
    affected_files: list[str]
    fix_strategy: str
    patch: str
    targeted_tests: list[str]
    risk: str

    def to_dict(self) -> dict:
        return {
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "affected_files": list(self.affected_files),
            "fix_strategy": self.fix_strategy,
            "patch": self.patch,
            "targeted_tests": list(self.targeted_tests),
            "risk": self.risk,
        }


@dataclass
class VerificationResult:
    """Outcome of running the targeted test and then the full test suite."""

    targeted_test: Optional[str] = None
    targeted_passed: bool = False
    full_suite_passed: bool = False
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "targeted_test": self.targeted_test,
            "targeted_passed": self.targeted_passed,
            "full_suite_passed": self.full_suite_passed,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": self.duration,
        }

    @property
    def success(self) -> bool:
        return self.targeted_passed and self.full_suite_passed


@dataclass
class FixOutcome:
    """Result of a full AutoFix run."""

    success: bool = False
    mode: str = "suggest"
    report: Optional[ErrorReport] = None
    proposal: Optional[FixProposal] = None
    verification: Optional[VerificationResult] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    error: Optional[str] = None
    record_paths: list[str] = field(default_factory=list)
    attempt: int = 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "mode": self.mode,
            "attempt": self.attempt,
            "report": self.report.to_dict() if self.report else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "branch": self.branch,
            "commit": self.commit,
            "error": self.error,
            "record_paths": self.record_paths,
        }


@dataclass
class AutoFixConfig:
    """Safe subset of autofix configuration."""

    enabled: bool = True
    mode: str = "suggest"
    max_attempts: int = 2
    allowed_paths: list[str] = field(default_factory=lambda: ["AIRA/", "tests/"])
    provider: str = "ollama"
    model: Optional[str] = None

    @classmethod
    def from_config(cls, config: Any) -> "AutoFixConfig":
        autofix_cfg = config.get("autofix") if hasattr(config, "get") else {}
        autofix_cfg = autofix_cfg or {}
        allowed = autofix_cfg.get("allowed_paths") or ["AIRA/", "tests/"]
        mode = str(autofix_cfg.get("mode", "suggest")).lower()
        if mode not in ("suggest", "safe"):
            mode = "suggest"
        return cls(
            enabled=bool(autofix_cfg.get("enabled", True)),
            mode=mode,
            max_attempts=int(autofix_cfg.get("max_attempts", 2) or 2),
            allowed_paths=[str(p) for p in allowed],
        )


class AutoFixError(Exception):
    """Raised when an AutoFix operation fails safely."""