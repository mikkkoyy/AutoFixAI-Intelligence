"""READ_ONLY workspace tools for the ChatGPT tool-calling layer.

Every tool here is strictly scoped to the active workspace:

- paths are resolved and MUST stay inside the workspace root,
- secret-looking files (.env, *.pem, *.key, id_rsa*, credentials*, …) are
  refused outright — their contents are never read, searched or returned,
- all text that leaves this module passes through ``redact_secrets``,
- reads are capped (lines + bytes) so one call can never dump a whole tree.

No tool in this module mutates anything.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath

from app.agents.task_memory import (
    KIND_ERRORS,
    memory_kind_dir,
    redact_secrets,
    retrieve_relevant,
)
from app.agents.tools.base import (
    E_INVALID_ARGUMENTS,
    E_NOT_FOUND,
    E_PATH_OUTSIDE_WORKSPACE,
    E_SECRET_FILE,
    Permission,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolSpec,
)

# ----------------------------------------------------------------------
# Caps
# ----------------------------------------------------------------------

MAX_READ_LINES = 400
MAX_READ_BYTES = 64 * 1024
DEFAULT_SEARCH_RESULTS = 25
MAX_SEARCH_RESULTS = 50
SEARCH_MAX_FILE_BYTES = 512 * 1024
SEARCH_MAX_FILES = 2000
INSPECT_MAX_ENTRIES = 100
LINE_DISPLAY_CAP = 240
GIT_STATUS_TIMEOUT_SECONDS = 6
GIT_STATUS_MAX_LINES = 40

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ----------------------------------------------------------------------
# Path safety
# ----------------------------------------------------------------------


def resolve_in_workspace(workspace: Path, raw) -> Path:
    """Resolve *raw* strictly inside *workspace*; raise otherwise.

    Absolute paths are allowed only when they already point inside the
    workspace.  ``..`` escapes, other drives and symlink jumps outside are
    all rejected after resolution.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError(E_INVALID_ARGUMENTS, "A non-empty 'path' string is required.")

    root = Path(workspace).resolve()
    candidate = Path(raw.strip())
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    except OSError as exc:
        raise ToolError(E_INVALID_ARGUMENTS, f"Path could not be resolved: {exc}") from exc

    try:
        resolved.relative_to(root)
    except ValueError:
        raise ToolError(
            E_PATH_OUTSIDE_WORKSPACE,
            f"Path '{raw}' resolves outside the active workspace.",
        ) from None
    return resolved


#: Exact file names that are always treated as secrets.
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".npmrc",
    ".netrc",
    ".pypirc",
}

#: Suffixes that are always treated as secrets.
_SECRET_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx", ".keystore"}

#: Substrings in a file name that mark it as a secret.
_SECRET_NAME_PARTS = (
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credential",
    "secret",
    "password",
)


def is_secret_file(path: Path) -> bool:
    """True when the FILE NAME alone marks this file as secret-bearing."""
    lowered = path.name.lower()
    if lowered in _SECRET_NAMES:
        return True
    if lowered.startswith(".env"):
        return True
    if path.suffix.lower() in _SECRET_SUFFIXES:
        return True
    return any(part in lowered for part in _SECRET_NAME_PARTS)


#: Directories never scanned by search/inspect.
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".autofix",
    ".opencode",
    ".next",
    ".tox",
    "target",
}


def _iter_project_files(workspace: Path):
    """Yield project files under *workspace*, pruning ignored directories."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in IGNORED_DIRS)
        for name in sorted(filenames):
            count += 1
            if count > SEARCH_MAX_FILES:
                return
            yield Path(dirpath) / name


def _decode_head(path: Path, max_bytes: int) -> str | None:
    """Decode at most *max_bytes* of *path*; ``None`` when unreadable/binary."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes)
    except OSError:
        return None
    if b"\x00" in data:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def _clip(line: str) -> str:
    line = line.rstrip("\r\n\t ")
    if len(line) > LINE_DISPLAY_CAP:
        return line[:LINE_DISPLAY_CAP] + "…"
    return line


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------


_PROJECT_MARKERS = {
    "pytest.ini": "python-pytest",
    "pyproject.toml": "python-modern",
    "setup.py": "python-packaging",
    "requirements.txt": "python-pip",
    "package.json": "node-js",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java-maven",
    "build.gradle": "java-gradle",
    "composer.json": "php",
    "Gemfile": "ruby",
    "README.md": "documented",
}


def _inspect_handler(args: dict, context: ToolContext) -> dict:
    workspace = Path(context.workspace)
    result: dict = {"summary": "", "exists": workspace.is_dir()}

    if not workspace.is_dir():
        result["summary"] = f"Workspace does not exist: {workspace}"
        return result

    detected = [
        label
        for marker, label in _PROJECT_MARKERS.items()
        if (workspace / marker).is_file()
    ]
    result["project_types"] = detected or ["unknown"]

    entries: list[dict] = []
    try:
        names = sorted(
            os.listdir(workspace),
            key=lambda n: (not (workspace / n).is_dir(), n.lower()),
        )
    except OSError as exc:
        raise ToolError(E_NOT_FOUND, f"Workspace listing failed: {exc}") from exc

    shown = 0
    for name in names:
        if name.lower() in IGNORED_DIRS:
            continue
        path = workspace / name
        entry = {"name": name, "type": "dir" if path.is_dir() else "file"}
        if path.is_file():
            entry["bytes"] = path.stat().st_size
        entries.append(entry)
        shown += 1
        if shown >= INSPECT_MAX_ENTRIES:
            break

    total_entries = len(names)
    result["top_level"] = entries
    result["top_level_truncated"] = total_entries > INSPECT_MAX_ENTRIES

    git_status = _git_status_porcelain(workspace)
    if git_status is not None:
        result["git_status"] = git_status

    errors_dir = memory_kind_dir(workspace, KIND_ERRORS)
    try:
        recent_errors = len(list(errors_dir.glob("*.json"))) if errors_dir.is_dir() else 0
    except OSError:
        recent_errors = 0
    result["recent_error_records"] = recent_errors

    types = ", ".join(result["project_types"])
    result["summary"] = (
        f"{workspace.name}: type={types}, top-level entries listed={shown}"
        + (f"/{total_entries}" if total_entries > INSPECT_MAX_ENTRIES else "")
        + (f", git changes={len(git_status.splitlines())}" if git_status else "")
    )
    return result


def _git_status_porcelain(workspace: Path) -> str | None:
    if not (workspace / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_STATUS_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = [redact_secrets(_clip(line)) for line in completed.stdout.splitlines()]
    lines = lines[:GIT_STATUS_MAX_LINES]
    return "\n".join(lines) if lines else "(clean)"


def _search_handler(args: dict, context: ToolContext) -> dict:
    query = args["query"]
    max_results = int(args.get("max_results") or DEFAULT_SEARCH_RESULTS)
    needle = query.casefold()

    scope_root = resolve_in_workspace(context.workspace, args.get("path_scope", "."))
    if not scope_root.is_dir():
        raise ToolError(E_NOT_FOUND, f"path_scope is not a directory: {args.get('path_scope')}")

    matches: list[dict] = []
    truncated = False
    for path in _iter_project_files(scope_root):
        if len(matches) >= max_results:
            truncated = True
            break
        if is_secret_file(path):
            continue
        try:
            if path.stat().st_size > SEARCH_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        text = _decode_head(path, SEARCH_MAX_FILE_BYTES)
        if text is None:
            continue
        rel = PurePosixPath(path.relative_to(context.workspace.resolve()).as_posix())
        for line_number, line in enumerate(text.splitlines(), start=1):
            if needle in line.casefold():
                matches.append(
                    {
                        "file": str(rel),
                        "line": line_number,
                        "text": redact_secrets(_clip(line)),
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break

    return {
        "summary": (
            f"{len(matches)} match(es) for '{query}'"
            + (" (result cap reached)" if truncated else "")
        ),
        "matches": matches,
        "truncated": truncated,
    }


def _read_handler(args: dict, context: ToolContext) -> dict:
    resolved = resolve_in_workspace(context.workspace, args.get("path"))
    if not resolved.exists():
        raise ToolError(E_NOT_FOUND, f"No such file: {args.get('path')}")
    if resolved.is_dir():
        raise ToolError(E_INVALID_ARGUMENTS, f"'{args.get('path')}' is a directory.")
    if is_secret_file(resolved):
        raise ToolError(
            E_SECRET_FILE,
            f"'{resolved.name}' looks like a credentials/secret file; "
            "reading it is not permitted.",
        )

    raw_lines: list[str]
    truncated_by_bytes = False
    try:
        with open(resolved, "rb") as handle:
            head = handle.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        raise ToolError(E_NOT_FOUND, f"File could not be read: {exc}") from exc
    if b"\x00" in head:
        raise ToolError(E_INVALID_ARGUMENTS, "Refusing to read a binary file as text.")
    truncated_by_bytes = len(head) > MAX_READ_BYTES
    raw_lines = head[:MAX_READ_BYTES].decode("utf-8", errors="replace").splitlines()

    total_lines = len(raw_lines)
    start_line = int(args.get("start_line") or 1)
    end_line = args.get("end_line")
    window = MAX_READ_LINES
    if end_line is not None:
        end_line = min(int(end_line), start_line + window - 1, total_lines)
        start_line = min(start_line, end_line)
    else:
        end_line = min(start_line + window - 1, total_lines)

    selected = raw_lines[start_line - 1 : end_line]
    content = "\n".join(redact_secrets(_clip(line)) for line in selected)

    notes = []
    if end_line < total_lines or truncated_by_bytes:
        notes.append("output truncated by read caps — page with start_line")

    return {
        "summary": (
            f"Read {resolved.name} lines {start_line}–{end_line} "
            f"of {total_lines}" + (" (capped)" if notes else "")
        ),
        "file": str(PurePosixPath(resolved.relative_to(context.workspace.resolve()).as_posix())),
        "content": content,
        "total_lines": total_lines,
        "start_line": start_line,
        "end_line": end_line,
        "note": "; ".join(notes) or None,
    }


_MEMORY_LIMIT_DEFAULT = 5


def _memory_search_handler(args: dict, context: ToolContext) -> dict:
    limit = int(args.get("limit") or _MEMORY_LIMIT_DEFAULT)
    records = retrieve_relevant(Path(context.workspace), args["query"], limit=limit)
    slim = [
        {
            "kind": record.get("kind"),
            "title": record.get("title"),
            "created_at": record.get("created_at"),
            "tags": record.get("tags") or [],
            "content": redact_secrets(str(record.get("content", ""))[:400]),
        }
        for record in records
    ]
    return {
        "summary": (
            f"{len(slim)} project-memory record(s) relevant to '{args['query']}'"
            if slim
            else "No matching project-memory records."
        ),
        "records": slim,
    }


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

_WORKSPACE_INSPECT_SCHEMA = {"type": "object", "properties": {}}

_WORKSPACE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "path_scope": {"type": "string", "minLength": 1, "maxLength": 500},
        "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
    },
}

_WORKSPACE_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 500},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    },
}

_MEMORY_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 2, "maxLength": 300},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
    },
}


def register_workspace_tools(registry: ToolRegistry) -> None:
    """Register every READ_ONLY workspace/memory tool."""
    registry.register(
        ToolSpec(
            name="workspace_inspect",
            description=(
                "Inspect the active workspace: detected project type markers, "
                "top-level files/folders, optional git status and the number of "
                "recent error records. Read-only."
            ),
            parameters=_WORKSPACE_INSPECT_SCHEMA,
            handler=_inspect_handler,
            permission=Permission.READ_ONLY,
            result_required=("summary",),
        )
    )
    registry.register(
        ToolSpec(
            name="workspace_search",
            description=(
                "Case-insensitive text search across project files inside the "
                "active workspace. Secret-looking files are skipped. Returns "
                "file:line:text matches, capped by max_results. Read-only."
            ),
            parameters=_WORKSPACE_SEARCH_SCHEMA,
            handler=_search_handler,
            permission=Permission.READ_ONLY,
            result_required=("summary", "matches"),
        )
    )
    registry.register(
        ToolSpec(
            name="workspace_read",
            description=(
                "Read a slice of ONE text file inside the active workspace "
                "(max 400 lines / 64KB per call; page via start_line). "
                "Secret-looking files are refused. Read-only."
            ),
            parameters=_WORKSPACE_READ_SCHEMA,
            handler=_read_handler,
            permission=Permission.READ_ONLY,
            result_required=("summary", "content"),
        )
    )
    registry.register(
        ToolSpec(
            name="project_memory_search",
            description=(
                "Keyword search over THIS project's persisted AutoFix memory "
                "(past fixes, errors, decisions). Only records belonging to the "
                "active workspace are visible. Read-only."
            ),
            parameters=_MEMORY_SEARCH_SCHEMA,
            handler=_memory_search_handler,
            permission=Permission.READ_ONLY,
            result_required=("summary", "records"),
        )
    )
