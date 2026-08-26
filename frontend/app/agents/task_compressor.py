"""Small task-compression utility for Bulk → AutoFix.

When a Bulk submission contains 20 or more lines, generate a short
single-word uppercase label that summarizes the task (without altering
or discarding the original request). The compressor is conservative and
relies on simple keyword heuristics to produce human-meaningful labels.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

KEYWORD_LABELS = [
    (r"\bauth|authentication|login|signin|signup|oauth\b", "AUTHFIX"),
    (r"\b(database|db|sql|postgres|mysql|sqlite|mongo)\b", "DATABASEFIX"),
    (r"\b(ui|ux|interface|button|modal|dialog|layout|style|css)\b", "UIFIX"),
    (r"\bterminal|console|shell|pwsh|cmd\b", "TERMINALFIX"),
    (r"\bopencode\b", "OPENCODEFIX"),
    (r"\bmemory|persist|persisting|task memory|.autofix\\memory\b", "MEMORY"),
    (r"\brefactor|refactoring\b", "REFACTOR"),
    (r"\bfix|bug|error|debug|crash|traceback|exception\b", "DEBUG"),
    (r"\boptimi[sz]e|performance|speed|fast\b", "OPTIMIZE"),
    (r"\bfeature|add|implement|create|generate\b", "FEATURE"),
]

FALLBACK_LABEL = "TASK"


def compress_task(request: str) -> Optional[str]:
    """Return a single-word uppercase label when the *request* has 20+ lines.

    Returns None when the request is fewer than 20 lines.
    The returned label is a single token, uppercase, and contains no spaces.
    """
    if request is None:
        return None
    # Count the longest contiguous block of non-empty lines. This treats
    # a large pasted code block (many contiguous non-empty lines) as the
    # trigger for compression while ignoring short header/footer text.
    lines = request.splitlines()
    longest = 0
    cur = 0
    for l in lines:
        if l.strip():
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 0
    if longest < 20:
        return None

    text = request.lower()
    for pattern, label in KEYWORD_LABELS:
        if re.search(pattern, text):
            return label

    # Heuristic fallback: try to extract a short noun from the first line
    first = lines[0].strip()
    if first:
        # Pick the first alpha word, strip non-word chars
        m = re.search(r"([A-Za-z0-9]{3,})", first)
        if m:
            token = m.group(1).upper()
            token = re.sub(r"[^A-Z0-9]", "", token)
            if token:
                # Keep it short
                return token[:16]

    return FALLBACK_LABEL
