"""AIRA AutoFix v1: safe, AI-assisted automated bug fixing.

Modes:
  suggest - analyze a failure and produce a fix proposal (no file changes)
  safe    - apply the fix on an isolated git branch, verify tests, rollback on failure
"""

from AIRA.autofix.analyzer import AnalysisError, AIAnalyzer
from AIRA.autofix.engine import AutoFixEngine
from AIRA.autofix.fixer import PatchSafetyError, validate_patch
from AIRA.autofix.models import (
    AutoFixConfig,
    AutoFixError,
    ErrorReport,
    FixOutcome,
    FixProposal,
    VerificationResult,
)
from AIRA.autofix.monitor import ErrorMonitor, SecretRedactor, normalize_error
from AIRA.autofix.rollback import RollbackManager
from AIRA.autofix.verifier import Verifier

__all__ = [
    "AnalysisError",
    "AIAnalyzer",
    "AutoFixConfig",
    "AutoFixEngine",
    "AutoFixError",
    "ErrorMonitor",
    "ErrorReport",
    "FixOutcome",
    "FixProposal",
    "PatchSafetyError",
    "RollbackManager",
    "SecretRedactor",
    "VerificationResult",
    "Verifier",
    "normalize_error",
    "validate_patch",
]