"""Safe rollback for a failed AutoFix attempt.

Rollback restores only the files the fix touched and leaves every other
working-tree change (including the user's pre-existing uncommitted changes)
untouched. An isolated autofix branch is deleted after being restored so no
partial fix state survives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from AIRA.autofix.fixer import (
    GitCommandError,
    _ProcessRunner,
    _run_git,
    _runner,
    switch_branch,
)
from AIRA.core.logging import get_logger

logger = get_logger("autofix")


def to_repo_relative(repo_root: Path, changed_files: list[Path] | list[str]) -> list[str]:
    root = Path(repo_root).resolve()
    relative = []
    for item in changed_files:
        path = Path(item)
        try:
            relative.append(str(path.resolve().relative_to(root)).replace("\\", "/"))
        except ValueError:
            relative.append(str(path).replace("\\", "/"))
    return relative


class RollbackManager:
    """Restores the repository after a failed autofix branch."""

    def __init__(self, repo_root: Path, runner: Optional[_ProcessRunner] = None):
        self.repo_root = Path(repo_root)
        self.runner = runner or _runner()

    def rollback(
        self,
        branch: str,
        original_branch: str,
        changed_files: list[Path] | list[str] | None = None,
        base_commit: Optional[str] = None,
    ) -> None:
        if not original_branch:
            raise GitCommandError("Cannot rollback without an original branch name")

        changed = to_repo_relative(self.repo_root, changed_files or [])

        if changed and len(branch) > 0:
            proc = _run_git(self.runner, self.repo_root, "checkout", "--", *changed)
            if proc.returncode != 0:
                logger.error(f"Rollback restore of {changed} failed: {proc.stderr}")
                raise GitCommandError(f"Failed to restore changed files: {proc.stderr}")

        if branch:
            try:
                switch_branch(self.repo_root, original_branch, self.runner)
            except GitCommandError:
                raise

        if branch:
            proc = _run_git(self.runner, self.repo_root, "branch", "-D", branch)
            if proc.returncode != 0:
                logger.error(f"Rollback branch deletion failed: {proc.stderr}")
                raise GitCommandError(f"Failed to delete autofix branch '{branch}': {proc.stderr}")

        logger.info(
            f"Rollback complete: restored {branch or 'worktree'} -> {original_branch}"
        )

    def restore_branch(self, original_branch: str) -> None:
        switch_branch(self.repo_root, original_branch, self.runner)