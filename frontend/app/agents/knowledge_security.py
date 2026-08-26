"""Security vetting for shared AI knowledge.

Before ANY content can be proposed for the shared GitHub AI-knowledge
repository it is scanned for secret-like material:

- hard secrets  → the proposal is BLOCKED (never saved, never displayed raw)
- soft secrets  → values are redacted (key names survive, values do not)

This module never displays or logs the secret material itself — findings are
reported as TYPE LABELS only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.task_memory import redact_secrets

# Hard secrets make content unsuitable for sharing even after redaction:
# private key blocks, JWTs and cookie/session stores carry material that a
# regex-redact could mangle but not neutralize reliably.
_HARD_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "private key block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r"(?:(?:.|\n)*?-----END [A-Z ]*PRIVATE KEY-----)?"
        ),
    ),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    (
        "session cookie",
        re.compile(
            r"\b(sessionid|session_id|csrftoken|auth[_-]?cookie)\s*[:=]\s*\S+",
            re.IGNORECASE,
        ),
    ),
    (
        "connection string with credentials",
        re.compile(
            r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s]+", re.IGNORECASE
        ),
    ),
)

# Soft secrets are redacted in place (same policy as project memory).
_SOFT_SECRET_LABELS: tuple[str, ...] = (
    "API key",
    "bearer token",
    "credential assignment",
)


@dataclass
class KnowledgeVetting:
    """Outcome of the security scan for one knowledge candidate."""

    ok: bool                          # False → blocked, must not be saved
    blocked_reasons: list = field(default_factory=list)
    findings: list = field(default_factory=list)   # safe type labels only
    sanitized_title: str = ""
    sanitized_body: str = ""
    was_sanitized: bool = False


def detect_secret_types(text: str) -> list[str]:
    """Type labels of secret-looking material in *text* (labels ONLY)."""
    labels: list[str] = []
    value = text or ""
    for label, pattern in _HARD_SECRET_PATTERNS:
        if pattern.search(value):
            labels.append(label)
    lowered = value.lower()
    if "sk-" in value or "ghp_" in value or "github_pat_" in value:
        labels.append("API key")
    elif "bearer " in lowered:
        labels.append("bearer token")
    if re.search(
        r"\b(api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]", lowered
    ):
        labels.append("credential assignment")
    # De-duplicate while keeping first-seen order.
    seen: set[str] = set()
    unique = [label for label in labels if not (label in seen or seen.add(label))]
    return unique


def sanitize_text(text: str) -> str:
    """Redact soft secret values; drop hard-secret constructs entirely."""
    result = redact_secrets(text or "")
    for _label, pattern in _HARD_SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def vet_knowledge(title: str, body: str) -> KnowledgeVetting:
    """Scan title/body; return a veto/sanitize verdict + clean versions."""
    combined = f"{title or ''}\n{body or ''}"
    hard_hits = [
        label
        for label, pattern in _HARD_SECRET_PATTERNS
        if pattern.search(combined)
    ]
    findings = detect_secret_types(combined)

    clean_title = sanitize_text(title or "")
    clean_body = sanitize_text(body or "")
    was_sanitized = (clean_title != (title or "")) or (clean_body != (body or ""))

    if hard_hits:
        return KnowledgeVetting(
            ok=False,
            blocked_reasons=[
                f"contains {label}" for label in hard_hits
            ],
            findings=findings,
            sanitized_title=clean_title,
            sanitized_body=clean_body,
            was_sanitized=True,
        )

    return KnowledgeVetting(
        ok=True,
        blocked_reasons=[],
        findings=findings,
        sanitized_title=clean_title,
        sanitized_body=clean_body,
        was_sanitized=was_sanitized,
    )
