"""AutoFix engine: orchestrates analysis, safe application, verification, and learning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from AIRA.autofix.analyzer import AIAnalyzer
from AIRA.autofix.fixer import (
    PatchSafetyError,
    apply_patch,
    commit_changes,
    create_branch,
    current_branch,
    dirty_changes_for_file,
    head_commit,
    make_branch_name,
    patch_target_paths,
    validate_patch,
)
from AIRA.autofix.models import (
    AutoFixConfig,
    AutoFixError,
    ErrorReport,
    FixOutcome,
    FixProposal,
    VerificationResult,
)
from AIRA.autofix.rollback import RollbackManager
from AIRA.autofix.verifier import Verifier
from AIRA.core.logging import get_logger
from AIRA.core.models import timestamp_now

logger = get_logger("autofix")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class AutoFixEngine:
    """Coordinates a full AutoFix run in 'suggest' or 'safe' mode."""

    def __init__(
        self,
        config: Any = None,
        autofix_config: Optional[AutoFixConfig] = None,
        provider: Any = None,
        store: Any = None,
        repo_root: Optional[Path] = None,
        verifier: Optional[Verifier] = None,
        analyzer: Optional[AIAnalyzer] = None,
        rollback: Optional[RollbackManager] = None,
    ):
        self.repo_root = Path(repo_root or PROJECT_ROOT)

        if autofix_config is not None:
            self.autofix_config = autofix_config
        elif config is not None:
            self.autofix_config = AutoFixConfig.from_config(config)
        else:
            self.autofix_config = AutoFixConfig()

        from AIRA.intelligence import IntelligenceStore

        self.config = config
        self.store = store or IntelligenceStore(self.repo_root / "AIRA" / "intelligence")
        self.provider = provider or self._build_provider(config)
        self.analyzer = analyzer or AIAnalyzer(self.provider)
        self.verifier = verifier or Verifier(repo_root=self.repo_root)
        self.rollback = rollback or RollbackManager(self.repo_root)

    @staticmethod
    def _build_provider(config: Any):
        from AIRA.core.ai_provider import create_provider

        return create_provider(config)

    # ---- public API ----

    async def run(
        self,
        report: ErrorReport,
        mode: Optional[str] = None,
        test_name: Optional[str] = None,
        relevant_source: str = "",
        relevant_tests: str = "",
        intelligence: str = "",
        repository_context: str = "",
    ) -> FixOutcome:
        if not self.autofix_config.enabled:
            return FixOutcome(
                success=False,
                report=report,
                error="AutoFix is disabled in the configuration",
            )

        mode = (mode or self.autofix_config.mode).strip().lower()
        if mode not in ("suggest", "safe"):
            return FixOutcome(
                success=False,
                report=report,
                error=f"Unsupported autofix mode '{mode}'",
            )

        prior_fixes = self._prior_fixes_context(report)
        last_test_output = ""

        for attempt in range(1, max(1, self.autofix_config.max_attempts) + 1):
            try:
                proposal = await self.analyzer.analyze(
                    report,
                    relevant_source=relevant_source,
                    relevant_tests=relevant_tests,
                    intelligence=intelligence,
                    repository_context=repository_context,
                    prior_fixes=prior_fixes,
                    test_output=last_test_output,
                )
            except AutoFixError as e:
                return FixOutcome(
                    success=False,
                    mode=mode,
                    report=report,
                    error=str(e),
                    attempt=attempt,
                )

            if mode == "suggest":
                return FixOutcome(
                    success=False,
                    mode=mode,
                    report=report,
                    proposal=proposal,
                    attempt=attempt,
                )

            outcome = await self._apply_and_verify(
                report=report,
                proposal=proposal,
                test_name=test_name,
                attempt=attempt,
            )
            if outcome.success:
                return outcome

            if outcome.verification is not None and not outcome.verification.success:
                last_test_output = self._tail_test_output(outcome.verification)
                if attempt >= max(1, self.autofix_config.max_attempts):
                    return outcome
                continue
            return outcome

        return FixOutcome(
            success=False,
            mode=mode,
            report=report,
            error="All autofix attempts failed verification",
            attempt=self.autofix_config.max_attempts,
        )

    # ---- safe-mode application ----

    async def _apply_and_verify(
        self,
        report: ErrorReport,
        proposal: FixProposal,
        test_name: Optional[str],
        attempt: int,
    ) -> FixOutcome:
        outcome = FixOutcome(
            success=False,
            mode="safe",
            report=report,
            proposal=proposal,
            attempt=attempt,
        )

        allowed = self.autofix_config.allowed_paths
        try:
            validate_patch(proposal.patch, allowed, self.repo_root)
        except PatchSafetyError as e:
            outcome.error = str(e)
            self._record_failed(report, proposal, None, error=str(e))
            return outcome

        target_test = test_name or (proposal.targeted_tests[0] if proposal.targeted_tests else None)
        if not target_test:
            self._record_failed(report, proposal, None, error="No targeted test available")
            outcome.error = "No targeted test available for verification. Provide --test."
            return outcome

        try:
            targets = patch_target_paths(proposal.patch, allowed, self.repo_root)
            for target in targets:
                rel = str(target.relative_to(self.repo_root.resolve())).replace("\\", "/")
                if dirty_changes_for_file(self.repo_root, rel):
                    raise PatchSafetyError(
                        f"Refusing to overwrite pre-existing changes to '{rel}'. Commit or stash them first."
                    )
        except PatchSafetyError as e:
            outcome.error = str(e)
            self._record_failed(report, proposal, None, error=str(e))
            return outcome

        original_branch = current_branch(self.repo_root)
        if not original_branch:
            self._record_failed(report, proposal, None, error="Repository is not on a git branch")
            outcome.error = "Repository is not on a git branch; refusing safe mode."
            return outcome

        base_commit = head_commit(self.repo_root)
        branch = make_branch_name("autofix")
        created_branch = False
        changed_files: list[Path] = []
        verification: Optional[VerificationResult] = None

        try:
            create_branch(self.repo_root, branch)
            created_branch = True
            changed_files = apply_patch(proposal.patch, self.repo_root, allowed, raise_on_dirty=True)

            verification = self.verifier.verify(target_test)
            outcome.verification = verification

            if verification.success:
                commit = commit_changes(
                    self.repo_root,
                    changed_files,
                    f"autofix: {report.error_type} - {report.message[:60]}",
                )
                record = self._record_fix(report, proposal, verification, commit)
                outcome.success = True
                outcome.commit = commit
                outcome.branch = branch
                outcome.record_paths.append(str(record))
                logger.info(f"AutoFix applied on branch '{branch}' commit {commit}")
                return outcome

            self.rollback.rollback(branch, original_branch, changed_files, base_commit=base_commit)
            failed_test = verification.targeted_test
            outcome.error = f"Fix failed verification on {failed_test} and was rolled back"
            outcome.branch = None
            self._record_failed(
                report,
                proposal,
                verification,
                error=f"Verification failed on {failed_test}",
            )
            return outcome

        except Exception as e:
            outcome.error = str(e)
            logger.error(f"AutoFix safe-mode failure: {e}")
            if created_branch:
                try:
                    self.rollback.rollback(branch, original_branch, changed_files, base_commit=base_commit)
                except Exception as rollback_error:
                    outcome.error = f"{e} (rollback also failed: {rollback_error})"
            self._record_failed(report, proposal, verification, error=str(outcome.error))
            return outcome

    # ---- recording & context ----

    def _record_fix(
        self,
        report: ErrorReport,
        proposal: FixProposal,
        verification: VerificationResult,
        commit: str,
    ) -> Any:
        provider_name, model = self._provider_identity()
        record = {
            "error_signature": report.error_signature,
            "error_type": report.error_type,
            "message": report.message,
            "root_cause": proposal.root_cause,
            "affected_files": proposal.affected_files,
            "fix_strategy": proposal.fix_strategy,
            "tests": proposal.targeted_tests,
            "verified": True,
            "provider": provider_name,
            "model": model,
            "commit": commit,
            "timestamp": timestamp_now(),
        }
        path = self.store.save_fix(record)
        logger.info(f"Recorded successful fix: {path}")
        return path

    def _record_failed(
        self,
        report: ErrorReport,
        proposal: Optional[FixProposal],
        verification: Optional[VerificationResult],
        error: str,
    ) -> Any:
        provider_name, model = self._provider_identity()
        record = {
            "error_signature": report.error_signature,
            "error_type": report.error_type,
            "message": report.message,
            "root_cause": proposal.root_cause if proposal else None,
            "fix_strategy": proposal.fix_strategy if proposal else None,
            "affected_files": proposal.affected_files if proposal else [],
            "error": error,
            "failed_test": verification.targeted_test if verification else None,
            "test_error": verification.stderr[-1500:] if verification else None,
            "provider": provider_name,
            "model": model,
            "timestamp": timestamp_now(),
        }
        path = self.store.save_failed_attempt(record)
        logger.info(f"Recorded failed attempt: {path}")
        return path

    def _prior_fixes_context(self, report: ErrorReport) -> str:
        try:
            fixes = self.store.search_fixes(
                error_type=report.error_type, query=report.message[:80]
            )
        except Exception:
            return ""
        if not fixes:
            return ""
        return json.dumps(fixes[-5:], indent=2)[:4000]

    def _provider_identity(self) -> tuple[str, Optional[str]]:
        provider = self.provider
        name = type(provider).__name__
        for known in ("OpenAI", "Anthropic", "DeepSeek", "Ollama"):
            if name.startswith(known):
                return known.lower(), getattr(provider, "default_model", None)
        return name.lower(), getattr(provider, "default_model", None)

    @staticmethod
    def _tail_test_output(verification: VerificationResult) -> str:
        chunks = []
        if verification.stdout:
            chunks.append("STDOUT\n" + verification.stdout[-1500:])
        if verification.stderr:
            chunks.append("STDERR\n" + verification.stderr[-1500:])
        return "\n".join(chunks)[-3000:]