"""Project memory for AutoFix AI Studio.

Everything lives under ``<workspace>\\.autofix\\memory\\``::

    .autofix/memory/
        conversations/   relevant AI/project discussions
        tasks/           large-task records (request, plan, progress)
        sessions/        AutoFix/OpenCode session facts & termination state
        fixes/           successful fixes + verification results
        errors/          important failures and recovery information
        decisions/       architecture/design decisions & constraints
        index/           lightweight keyword index (built lazily)

Memory is written for RECOVERY as much as for history: interrupted work can
combine current task state with relevant previous errors, fixes and decisions.
Captured content is scanned for secret-looking values and redacted before it
touches disk.  Cleanup can ONLY ever operate below ``.autofix\\memory\\`` —
project source files are untouchable by construction (see ``cleanup_memory``
and ``delete_memory_paths``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIRNAME = ".autofix"
MEMORY_SUBDIR = "memory"

#: Memory categories (sub-directories of ``.autofix/memory``).
KIND_CONVERSATIONS = "conversations"
KIND_TASKS = "tasks"
KIND_SESSIONS = "sessions"
KIND_FIXES = "fixes"
KIND_ERRORS = "errors"
KIND_DECISIONS = "decisions"
KIND_INDEX = "index"
MEMORY_KINDS = (
    KIND_CONVERSATIONS,
    KIND_TASKS,
    KIND_SESSIONS,
    KIND_FIXES,
    KIND_ERRORS,
    KIND_DECISIONS,
    KIND_INDEX,
)

#: Retrieval priority — higher weight wins ties and boosts relevance.
_KIND_WEIGHTS = {
    KIND_ERRORS: 5,
    KIND_FIXES: 4,
    KIND_DECISIONS: 3,
    KIND_TASKS: 2,
    KIND_CONVERSATIONS: 2,
    KIND_SESSIONS: 1,
}

COMPLETED_STATUSES = ("completed",)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def memory_dir(workspace: str | Path) -> Path:
    return Path(workspace) / MEMORY_DIRNAME / MEMORY_SUBDIR


def _task_id(request: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(request.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{stamp}-{digest}"


def save_task_record(
    workspace: str | Path,
    request: str,
    *,
    status: str = "received",
    **extra,
) -> Path:
    """Persist a new task record and return its path."""
    directory = memory_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)

    record = {
        "id": _task_id(request),
        "kind": "autofix-large-task",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "status": status,
        "workspace": str(workspace),
        "request": request,
        "stats": {
            "characters": len(request),
            "lines": len(request.splitlines()),
        },
        "plan": None,
        "stages": [],
        "backend": None,
        "errors": [],
        "verified": None,
        "remaining": None,
    }
    record.update(extra)

    path = directory / f"task-{record['id']}.json"
    path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def update_task_record(path: str | Path, **fields) -> Path:
    """Merge *fields* into an existing record (updates ``updated_at``)."""
    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path

    stages = fields.pop("append_stage", None)
    errors = fields.pop("append_error", None)

    if stages is not None:
        record.setdefault("stages", []).append(stages)
    if errors is not None:
        record.setdefault("errors", []).append(errors)

    record.update(fields)
    record["updated_at"] = _now_iso()

    path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def load_task_record(path: str | Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_incomplete_tasks(workspace: str | Path) -> list[dict]:
    """All records that did not complete — the recovery candidate list."""
    directory = memory_dir(workspace)
    if not directory.is_dir():
        return []

    records: list[dict] = []
    for path in sorted(directory.glob("task-*.json")):
        record = load_task_record(path)
        if record and record.get("status") not in COMPLETED_STATUSES:
            record["_path"] = str(path)
            records.append(record)
    return records


def describe_remaining(record: dict) -> str:
    """One-paragraph recovery description for an incomplete task."""
    failed_stages = [
        stage.get("stage", "?")
        for stage in record.get("stages", [])
        if not stage.get("ok", True)
    ]
    parts = [f"status={record.get('status', 'unknown')}"]
    if failed_stages:
        parts.append("failed stages: " + ", ".join(failed_stages))
    if record.get("verified") is False:
        parts.append("verification did not pass")
    return "; ".join(parts)


# ----------------------------------------------------------------------
# Secret redaction
# ----------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{10,}", re.IGNORECASE),
    re.compile(
        r"\b((?:api[_-]?key|secret|token|password|passwd|pwd)"
        r'\s*[:=]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s"\']+)',
        re.IGNORECASE,
    ),
)

_REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Redact secret-looking values before anything is persisted."""
    result = text or ""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 1:
            # Keep the key name ("api_key="), drop the value.
            result = pattern.sub(lambda m: (m.group(1) or "") + _REDACTED, result)
        else:
            result = pattern.sub(_REDACTED, result)
    return result


# ----------------------------------------------------------------------
# Generic memory records
# ----------------------------------------------------------------------

def memory_kind_dir(workspace: str | Path, kind: str) -> Path:
    if kind not in MEMORY_KINDS:
        raise ValueError(f"Unknown memory kind: {kind!r}")
    return memory_dir(workspace) / kind


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "record").lower()).strip("-")
    return slug[:48] or "record"


def record_memory(
    workspace: str | Path,
    kind: str,
    title: str,
    content: str,
    *,
    tags: list[str] | None = None,
    **extra,
) -> Path:
    """Persist one memory record; content is redacted before storage."""
    directory = memory_kind_dir(workspace, kind)
    directory.mkdir(parents=True, exist_ok=True)

    record = {
        "kind": kind,
        "title": title,
        "created_at": _now_iso(),
        "tags": list(tags or []),
        "content": redact_secrets(content),
    }
    record.update(extra)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"{stamp}-{_slugify(title)}.json"
    path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def record_session(workspace: str | Path, *, session_id: str | None = None, **fields) -> Path:
    """Record an AutoFix/OpenCode session fact (timestamps, ids, outcome)."""
    title = fields.pop("title", session_id or "session")
    content = fields.pop("content", "")
    return record_memory(
        workspace, KIND_SESSIONS, title, content,
        session_id=session_id, **fields,
    )


def _iter_memory_records(workspace: str | Path):
    root = memory_dir(workspace)
    if not root.is_dir():
        return
    for kind in MEMORY_KINDS:
        directory = root / kind
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            record = load_task_record(path)
            if isinstance(record, dict):
                record["_path"] = str(path)
                record.setdefault("kind", kind)
                yield record


def retrieve_relevant(
    workspace: str | Path,
    query: str,
    limit: int = 5,
) -> list[dict]:
    """Deterministic keyword retrieval over project memory.

    Priority: errors → fixes → decisions → tasks/conversations → sessions.
    Only records whose text actually overlaps the query keywords are
    returned — memory is never injected wholesale.
    """
    keywords = re.findall(r"[a-z0-9_]{4,}", (query or "").lower())
    if not keywords:
        return []

    scored: list[tuple[int, str, dict]] = []
    for record in _iter_memory_records(workspace):
        haystack = " ".join(
            [
                str(record.get("title", "")),
                str(record.get("content", "")),
                " ".join(record.get("tags") or []),
                " ".join(str(v) for v in record.get("session_id", "") if v),
            ]
        ).lower()
        overlap = sum(1 for keyword in keywords if keyword in haystack)
        if overlap == 0:
            continue
        weight = _KIND_WEIGHTS.get(record.get("kind"), 1)
        scored.append((overlap * 10 + weight, str(record.get("created_at", "")), record))

    # Newest first on ties, then strongest match first (stable sorts).
    scored.sort(key=lambda item: item[1], reverse=True)
    scored.sort(key=lambda item: item[0], reverse=True)
    return [record for _score, _created, record in scored[:limit]]


# ----------------------------------------------------------------------
# Cleanup — restricted to <workspace>\.autofix\memory by construction
# ----------------------------------------------------------------------

def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def cleanup_memory(workspace: str | Path, older_than_days: float | None = None) -> int:
    """Delete files BELOW ``.autofix\\memory`` only.

    ``older_than_days=None`` sweeps the whole memory tree; a number keeps
    recent records.  Project source files are unreachable by construction:
    the traversal root IS the memory directory and every candidate is
    verified to resolve inside it before deletion.
    """
    root = memory_dir(workspace)
    if not root.is_dir():
        return 0

    cutoff = (
        None
        if older_than_days is None
        else time.time() - float(older_than_days) * 86400.0
    )

    removed = 0
    for path in list(root.rglob("*")):
        if not path.is_file():
            continue
        if not _is_within(path, root):
            continue
        try:
            # Strictly-newer files are kept.  ``<=`` would race with the
            # coarse Windows timer when older_than_days=0: a file created
            # microseconds before this call must still count as expired.
            if cutoff is not None and path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def delete_memory_paths(workspace: str | Path, paths) -> int:
    """Explicit deletion API for cleanup callers.

    Refuses ANY path that does not resolve inside ``<workspace>\\.autofix\\memory``.
    Project source files passed here are never deleted.
    """
    root = memory_dir(workspace)
    removed = 0
    for raw in paths or []:
        path = Path(raw)
        if not _is_within(path, root):
            continue
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
