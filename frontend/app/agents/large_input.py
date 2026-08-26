"""Automatic large-input detection for AutoFix.

A submitted task is classified as *large* purely by size — the user never
has to pick a special mode.  The threshold is configurable through the
``AUTOFIX_LARGE_INPUT_THRESHOLD`` environment variable (characters).

Large input is never truncated: detection only decides how the task is
processed and reported.  The complete text always reaches the planner,
the coding agent and the task memory record.
"""

from __future__ import annotations

import os

DEFAULT_LARGE_INPUT_THRESHOLD = 4000
_MIN_THRESHOLD = 1


def large_input_threshold(env=None) -> int:
    """Configured threshold in characters (falls back to the default)."""
    env = os.environ if env is None else env
    raw = str(env.get("AUTOFIX_LARGE_INPUT_THRESHOLD", "")).strip()
    if not raw:
        return DEFAULT_LARGE_INPUT_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LARGE_INPUT_THRESHOLD
    return value if value >= _MIN_THRESHOLD else DEFAULT_LARGE_INPUT_THRESHOLD


def is_large_input(text: str, env=None) -> bool:
    """True when *text* should be processed as a large AutoFix task."""
    return len(text or "") >= large_input_threshold(env)


def summarize_large_input(text: str, preview_lines: int = 12) -> str:
    """Compact human-readable summary of a large input.

    The summary intentionally does NOT embed the full text — the complete
    request lives in the task memory record and in the execution pipeline.
    """
    text = text or ""
    lines = text.splitlines()
    characters = len(text)
    line_count = len(lines)

    head = [line[:160] for line in lines[:preview_lines]]
    preview = "\n".join(head)
    hidden = line_count - len(head)
    if hidden > 0:
        preview += f"\n… ({hidden} more lines, complete text preserved)"

    return (
        f"{characters:,} characters, {line_count:,} lines\n"
        f"First {len(head)} lines:\n{preview}"
    )
