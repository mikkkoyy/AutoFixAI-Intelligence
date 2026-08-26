"""Large-task transport for coding-agent invocations.

Problem this solves
-------------------
Coding CLIs are invoked as ``opencode run "<prompt>"`` — the whole task text
becomes ONE command-line argument.  Windows ``CreateProcess`` caps a command
line at 32 767 characters; larger prompts fail to spawn or arrive garbled,
which surfaced as ``opencode exited with code 1`` before the agent could do
any work.

Design
------
The complete task payload is persisted inside the workspace:

    <workspace>\\.autofix\\tasks\\<task_id>.json

and the CLI receives only a COMPACT bootstrap instruction that points the
agent at that file.  The agent reads the authoritative payload from disk and
performs the complete request.  Nothing is truncated, split, or summarized.

Small prompts (<= ``AUTOFIX_INLINE_PROMPT_LIMIT`` characters) are still passed
inline exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

AUTOFIX_DIRNAME = ".autofix"
TASKS_DIRNAME = "tasks"

#: Prompts up to this many characters travel inline as before.  The Windows
#: command-line ceiling is 32 767 chars — stay far below it (the limit also
#: protects against slow argv handling in npm .cmd shims).
DEFAULT_INLINE_PROMPT_LIMIT = 2000


def inline_prompt_limit(env=None) -> int:
    env = os.environ if env is None else env
    raw = str(env.get("AUTOFIX_INLINE_PROMPT_LIMIT", "")).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INLINE_PROMPT_LIMIT
    return value if value > 0 else DEFAULT_INLINE_PROMPT_LIMIT


def tasks_dir(workspace: str | Path) -> Path:
    return Path(workspace) / AUTOFIX_DIRNAME / TASKS_DIRNAME


@dataclass
class TransportPlan:
    """Result of preparing a prompt for CLI transport."""

    #: What is actually passed on the command line (compact when transported).
    command_prompt: str
    #: Full payload path when transported, else None.
    payload_path: Path | None
    task_id: str | None
    #: True when the payload was written to disk instead of the argv.
    transported: bool


def new_task_id(content: str, prefix: str = "task") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{prefix}-{stamp}-{digest}"


def bootstrap_instruction(payload_path: Path, workspace: str | Path) -> str:
    """Compact instruction telling the agent where the complete task lives."""
    try:
        relative = Path(payload_path).resolve().relative_to(
            Path(workspace).resolve()
        ).as_posix()
    except ValueError:
        relative = str(payload_path)
    return (
        "AutoFix task hand-off: the COMPLETE task instructions are stored in "
        f"the file '{relative}' relative to the current working directory "
        "(an absolute fallback path is embedded in the file itself). Read "
        "that file first and perform the ENTIRE task it describes. The file "
        "is the authoritative source — never truncate, summarize or skip any "
        "part of the request it contains."
    )


def write_task_payload(
    workspace: str | Path,
    content: dict,
    *,
    task_id: str | None = None,
) -> tuple[Path, str]:
    """Persist a complete task payload under ``<workspace>\\.autofix\\tasks``."""
    task_id = task_id or new_task_id(json.dumps(content, sort_keys=True)[:512])
    directory = tasks_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
    }
    record.update(content)
    path = directory / f"{task_id}.json"
    path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path, task_id


def load_task_payload(path: str | Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prepare_task_payload(
    prompt: str,
    workspace: str | Path,
    *,
    extra_context: dict | None = None,
    payload_path: str | Path | None = None,
    env=None,
) -> TransportPlan:
    """Return the safe command-line prompt for *prompt*.

    Small prompts pass through unchanged.  Large prompts are persisted in
    full to ``<workspace>\\.autofix\\tasks\\<id>.json`` and replaced by a
    compact bootstrap instruction referencing that file.
    """
    if len(prompt) <= inline_prompt_limit(env):
        return TransportPlan(prompt, None, None, False)

    if payload_path is not None and Path(payload_path).exists():
        path = Path(payload_path)
        try:
            task_id = path.stem
        except Exception:
            task_id = None
    else:
        task_id = None
        content = {"kind": "autofix-task-payload", "request": prompt}
        if extra_context:
            content.update(extra_context)
        path, task_id = write_task_payload(workspace, content, task_id=task_id)

    return TransportPlan(
        command_prompt=bootstrap_instruction(path, workspace),
        payload_path=path,
        task_id=task_id,
        transported=True,
    )
