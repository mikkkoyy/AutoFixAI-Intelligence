"""Safe patch application for AutoFix.

The fixer only ever modifies files that pass validation. It rejects secrets,
credentials, keys, environment files, Windows system paths, and any path
outside the repository. In safe mode it creates an isolated git branch before
touching anything, and refuses to overwrite unrelated pre-existing changes.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from AIRA.autofix.models import AutoFixError
from AIRA.core.logging import get_logger
from AIRA.core.models import generate_short_id, timestamp_now

logger = get_logger("autofix")

FORBIDDEN_COMPONENTS = {
    ".git",
    ".venv",
    ".hg",
    ".svn",
    ".env",
    "credentials",
    "secrets",
}

FORBIDDEN_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".keychain",
    ".ppk",
)

WINDOWS_SYSTEM_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]windows(?:[\\/]|$)", re.IGNORECASE),
    re.compile(r"^[A-Za-z]:[\\/]program files(?:[\\/]|$)", re.IGNORECASE),
    re.compile(r"system32", re.IGNORECASE),
    re.compile(r"[\\/]programdata", re.IGNORECASE),
)

RunCommand = Callable[..., Any]


class PatchSafetyError(AutoFixError):
    """Raised when a proposed patch violates safety rules."""


class PatchApplyError(AutoFixError):
    """Raised when a validated patch cannot be applied cleanly."""


class GitCommandError(AutoFixError):
    """Raised when a git safety operation fails."""


class _ProcessRunner:
    """Shared helper so tests can substitute the process spawner."""

    def __init__(self, run_command: Optional[RunCommand] = None):
        self._run_command = run_command
        self._lock = threading.Lock()

    def run(self, cmd: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
        if self._run_command is not None:
            return self._run_command(cmd, cwd)
        with self._lock:
            return subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )


_default_runner = _ProcessRunner()


def bind_runner(runner: Optional[_ProcessRunner]) -> None:
    """Install a custom process runner (test seam)."""
    global _default_runner
    _default_runner = runner or _ProcessRunner()


def _runner() -> _ProcessRunner:
    return _default_runner


def _run_git(runner: _ProcessRunner, repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return runner.run(["git", *args], cwd=repo_root)


# ---- path safety ----

def is_forbidden_path(rel_path: str) -> bool:
    """Return True if the repository-relative path is never allowed."""
    normalized = rel_path.strip().replace("\\", "/")
    if not normalized:
        return True
    if re.match(r"^[A-Za-z]:", normalized):
        return True
    if normalized.startswith("/"):
        return True
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return True
    lowered_parts = [p for p in parts if p]
    for component in FORBIDDEN_COMPONENTS:
        if any(component == part.lower() for part in lowered_parts):
            return True
    for part in lowered_parts:
        lower = part.lower()
        for forbidden in FORBIDDEN_SUFFIXES:
            if lower.endswith(forbidden):
                return True
        stem = lower.split(".")[0]
        if "credentials" in stem or "secret" in stem:
            return True
    for pattern in WINDOWS_SYSTEM_PATTERNS:
        if pattern.search(normalized):
            return True
    return False


def resolve_target(repo_root: Path, rel_path: str) -> Path:
    """Resolve a repository-relative path, rejecting anything outside the repo."""
    if is_forbidden_path(rel_path):
        raise PatchSafetyError(f"Refusing to modify forbidden path: {rel_path}")
    repo_root = repo_root.resolve()
    candidate = (repo_root / rel_path).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        raise PatchSafetyError(f"Path is outside the repository: {rel_path}")
    return candidate


def is_allowed_path(rel_path: str, allowed_paths: list[str]) -> bool:
    normalized = rel_path.strip().replace("\\", "/").lower()
    for allowed in allowed_paths or []:
        prefix = allowed.strip().replace("\\", "/").lower().rstrip("/") + "/"
        allowed_root = allowed.strip().replace("\\", "/").lower().rstrip("/")
        if normalized == allowed_root or normalized.startswith(prefix):
            return True
    return False


# ---- patch parsing / validation ----

def parse_patch(patch: str) -> list[dict]:
    """Parse the JSON patch string into a list of file edit operations."""
    if not patch or not patch.strip():
        raise PatchSafetyError("Patch is empty")
    try:
        data = json.loads(patch)
    except json.JSONDecodeError as e:
        raise PatchSafetyError(f"Patch is not valid JSON: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list) or not data["edits"]:
        raise PatchSafetyError("Patch must contain a non-empty 'edits' array")
    for edit in data["edits"]:
        if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
            raise PatchSafetyError("Each patch edit must have a string 'path'")
        replaces = edit.get("replace", [])
        if not isinstance(replaces, list):
            raise PatchSafetyError(f"Patch edit '{edit.get('path')}' replace must be a list")
        for item in replaces:
            if not isinstance(item, dict) or not all(k in item for k in ("old", "new")):
                raise PatchSafetyError(
                    f"Patch edit '{edit.get('path')}' replace items need 'old' and 'new'"
                )
    return data["edits"]


def validate_patch(
    patch: str,
    allowed_paths: list[str] | None = None,
    repo_root: Optional[Path] = None,
) -> list[str]:
    """Validate structure + safety; return the approved relative paths."""
    edits = parse_patch(patch)
    approved: list[str] = []
    for edit in edits:
        rel_path = edit["path"].strip().replace("\\", "/")
        if is_forbidden_path(rel_path):
            raise PatchSafetyError(f"Refusing to modify forbidden path: {rel_path}")
        if allowed_paths and not is_allowed_path(rel_path, allowed_paths):
            raise PatchSafetyError(
                f"Path '{rel_path}' is not in the allowed paths: {allowed_paths}"
            )
        if repo_root is not None:
            resolve_target(repo_root, rel_path)
        if rel_path not in approved:
            approved.append(rel_path)
    return approved


def patch_target_paths(
    patch: str,
    allowed_paths: list[str] | None = None,
    repo_root: Optional[Path] = None,
) -> list[Path]:
    approved = validate_patch(patch, allowed_paths, repo_root)
    if repo_root is None:
        return [Path(p) for p in approved]
    return [resolve_target(repo_root, p) for p in approved]


# ---- git state inspection ----

def _parse_status_line(line: str) -> Optional[str]:
    line = line.rstrip("\n")
    if not line:
        return None
    if line.startswith("??"):
        return line[3:].strip()
    path = line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def get_dirty_files(repo_root: Path, runner: Optional[_ProcessRunner] = None) -> list[str]:
    runner = runner or _runner()
    proc = _run_git(runner, repo_root, "status", "--porcelain")
    files = []
    for raw in (proc.stdout or "").splitlines():
        rel = _parse_status_line(raw)
        if rel:
            files.append(rel.replace("\\", "/"))
    return files


def is_repo_dirty(repo_root: Path, runner: Optional[_ProcessRunner] = None) -> bool:
    return bool(get_dirty_files(repo_root, runner))


def dirty_changes_for_file(repo_root: Path, rel_path: str, runner: Optional[_ProcessRunner] = None) -> bool:
    normalized = rel_path.strip().replace("\\", "/")
    for dirty in get_dirty_files(repo_root, runner):
        if dirty.lower() == normalized.lower():
            return True
    return False


def current_branch(repo_root: Path, runner: Optional[_ProcessRunner] = None) -> str:
    runner = runner or _runner()
    proc = _run_git(runner, repo_root, "branch", "--show-current")
    return (proc.stdout or "").strip()


def head_commit(repo_root: Path, runner: Optional[_ProcessRunner] = None) -> str:
    runner = runner or _runner()
    proc = _run_git(runner, repo_root, "rev-parse", "HEAD")
    return (proc.stdout or "").strip()


def create_branch(repo_root: Path, branch_name: str, runner: Optional[_ProcessRunner] = None) -> str:
    runner = runner or _runner()
    proc = _run_git(runner, repo_root, "checkout", "-b", branch_name)
    if proc.returncode != 0:
        raise GitCommandError(f"Failed to create branch '{branch_name}': {proc.stderr}")
    return branch_name


def switch_branch(repo_root: Path, branch_name: str, runner: Optional[_ProcessRunner] = None) -> None:
    runner = runner or _runner()
    proc = _run_git(runner, repo_root, "checkout", branch_name)
    if proc.returncode != 0:
        raise GitCommandError(f"Failed to switch to branch '{branch_name}': {proc.stderr}")


def commit_changes(repo_root: Path, files: list[Path], message: str,
                   runner: Optional[_ProcessRunner] = None) -> str:
    runner = runner or _runner()
    add = _run_git(runner, repo_root, "add", "--", *[str(f) for f in files])
    if add.returncode != 0:
        raise GitCommandError(f"git add failed: {add.stderr}")
    commit = _run_git(runner, repo_root, "commit", "-m", message)
    if commit.returncode != 0:
        raise GitCommandError(f"git commit failed: {commit.stderr}")
    return head_commit(repo_root, runner)


def hard_reset(repo_root: Path, commit: str, runner: Optional[_ProcessRunner] = None) -> None:
    runner = runner or _runner()
    proc = _run_git(runner, repo_root, "reset", "--hard", commit)
    if proc.returncode != 0:
        raise GitCommandError(f"git reset failed: {proc.stderr}")


def delete_branch(repo_root: Path, branch_name: str, runner: Optional[_ProcessRunner] = None) -> None:
    runner = runner or _runner()
    proc = _run_git(runner, repo_root, "branch", "-D", branch_name)
    if proc.returncode != 0:
        raise GitCommandError(f"git branch -D failed: {proc.stderr}")


# ---- patch application ----

def apply_patch(
    patch: str,
    repo_root: Path,
    allowed_paths: list[str] | None = None,
    *,
    raise_on_dirty: bool = True,
) -> list[Path]:
    """Validate then apply a patch to working tree files."""
    edits = parse_patch(patch)
    targets = patch_target_paths(patch, allowed_paths, repo_root)

    if raise_on_dirty:
        for rel_path in {e["path"].strip().replace("\\", "/") for e in edits}:
            if dirty_changes_for_file(repo_root, rel_path):
                raise PatchSafetyError(
                    f"Refusing to overwrite pre-existing changes to '{rel_path}'. "
                    "Stash or commit them first."
                )

    changed: list[Path] = []
    for edit in edits:
        rel_path = edit["path"].strip().replace("\\", "/")
        target = resolve_target(repo_root, rel_path)
        if not target.exists():
            raise PatchApplyError(f"Target file does not exist: {rel_path}")
        content = target.read_text(encoding="utf-8")
        for item in edit.get("replace", []):
            old, new = item["old"], item["new"]
            if old:
                if old not in content:
                    raise PatchApplyError(
                        f"Could not find expected text in '{rel_path}' while applying patch"
                    )
                content = content.replace(old, new, 1)
            else:
                content += new
        target.write_text(content, encoding="utf-8")
        if target not in changed:
            changed.append(target)

    for resolved in targets:
        if resolved not in changed:
            logger.warning(f"Patch validated '{resolved}' but applied no edits")
    return changed


def make_branch_name(prefix: str = "autofix") -> str:
    return f"{prefix}-{timestamp_now()[:10]}-{generate_short_id()}"