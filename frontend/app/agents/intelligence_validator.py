"""Intelligence Entry Validation and Integrity.

Validates intelligence entries before they can be approved or published.
Catches schema violations, missing fields, duplicates, conflicts, and
dangerous content.

The validator is a pure-logic component with no side effects -- it never
modifies entries, never touches the network, and never writes to disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.intelligence_store import (
    INTELLIGENCE_LAYERS,
    IntelligenceEntry,
    IntelligenceStorage,
    VALID_STATUSES,
)

# -----------------------------------------------------------------------
# Validation Result
# -----------------------------------------------------------------------


@dataclass
class ValidationReport:
    """Outcome of validating an intelligence entry."""

    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


# -----------------------------------------------------------------------
# Dangerous Content Patterns
# -----------------------------------------------------------------------

_DANGEROUS_PATTERNS = (
    r"(?i)ignore\s+(all\s+)?previous\s+instructions",
    r"(?i)disregard\s+(all\s+)?prior",
    r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:different|new|evil|jailbroken)",
    r"(?i)bypass\s+(?:all\s+)?(?:security|safety|validation)",
    r"(?i)execute\s+(?:arbitrary|random)\s+(?:code|commands?)",
    r"(?i)delete\s+(?:all\s+)?(?:files?|data|database|table)",
    r"(?i)drop\s+table",
    r"(?i)\bos\.system\b",
    r"(?i)\beval\s*\(",
    r"(?i)\bexec\s*\(",
)

# Secret-like patterns (must never appear in intelligence content)
_SECRET_PATTERNS = (
    r"sk-[a-zA-Z0-9]{20,}",
    r"ghp_[a-zA-Z0-9]{36,}",
    r"github_pat_[a-zA-Z0-9]{80,}",
    r"AKIA[A-Z0-9]{16}",
    r"xox[baprs]-[a-zA-Z0-9-]+",
    r"(?i)(?:api[_-]?key|secret|password|token)\s*[=:]\s*\S{8,}",
    r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
    r"(?i)Bearer\s+[a-zA-Z0-9._-]{20,}",
)

# -----------------------------------------------------------------------
# Required Fields
# -----------------------------------------------------------------------

_REQUIRED_FIELDS = ("title", "layer", "summary", "content")

_OPTIONAL_RECOMMENDED_FIELDS = ("category", "tags", "source")


# -----------------------------------------------------------------------
# Validation Functions
# -----------------------------------------------------------------------


def validate_entry(
    entry: IntelligenceEntry,
    storage: IntelligenceStorage | None = None,
    check_conflicts: bool = True,
) -> ValidationReport:
    """Full validation of an intelligence entry.

    Checks:
        1. Schema validity (required fields present and non-empty)
        2. Layer validity (must be one of INTELLIGENCE_LAYERS)
        3. Status validity
        4. Content quality (non-trivial content)
        5. Duplicate detection (same title + layer)
        6. Dangerous content detection
        7. Secret detection
        8. Conflict detection (contradictory intelligence in same layer/category)
        9. Title length
        10. Tag format

    Args:
        entry: The intelligence entry to validate.
        storage: Optional storage instance for duplicate/conflict detection.
        check_conflicts: Whether to check for conflicting entries (can be slow).

    Returns:
        ValidationReport with ok=True if all checks pass.
    """
    report = ValidationReport()

    # 1. Required fields
    for field_name in _REQUIRED_FIELDS:
        value = getattr(entry, field_name, None)
        if not value or (isinstance(value, str) and not value.strip()):
            report.add_error(f"Missing required field: {field_name}")

    # 2. Layer validity
    if entry.layer and entry.layer not in INTELLIGENCE_LAYERS:
        report.add_error(
            f"Invalid layer '{entry.layer}'. Must be one of: {', '.join(INTELLIGENCE_LAYERS)}"
        )

    # 3. Status validity
    if entry.status and entry.status not in VALID_STATUSES:
        report.add_error(
            f"Invalid status '{entry.status}'. Must be one of: {', '.join(VALID_STATUSES)}"
        )

    # 4. Content quality
    if entry.content and len(entry.content.strip()) < 10:
        report.add_warning("Content is very short (< 10 characters). Consider expanding.")

    if entry.summary and len(entry.summary.strip()) < 10:
        report.add_warning("Summary is very short (< 10 characters). Consider expanding.")

    # 5. Title length
    if entry.title and len(entry.title) > 200:
        report.add_error("Title exceeds 200 characters.")

    # 6. Tag format
    for tag in (entry.tags or ()):
        if not re.match(r"^[a-zA-Z0-9_-]{1,50}$", tag):
            report.add_warning(f"Tag '{tag}' contains unusual characters.")

    # 7. Duplicate detection
    if storage and entry.title and entry.layer:
        if storage.entry_exists(entry.title, entry.layer):
            report.add_error(
                f"Duplicate entry: an entry with title '{entry.title}' "
                f"already exists in layer '{entry.layer}'."
            )

    # 8. Dangerous content detection
    combined_text = f"{entry.title} {entry.content}"
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, combined_text):
            report.add_error("Content contains potentially dangerous instructions.")
            break

    # 9. Secret detection
    for pattern in _SECRET_PATTERNS:
        if re.search(pattern, combined_text):
            report.add_error("Content contains what appears to be a secret or credential.")
            break

    # 10. Conflict detection
    if check_conflicts and storage and entry.title and entry.layer:
        conflicts = _detect_conflicts(entry, storage)
        for conflict_msg in conflicts:
            report.add_warning(conflict_msg)

    # 11. Recommended fields
    for field_name in _OPTIONAL_RECOMMENDED_FIELDS:
        value = getattr(entry, field_name, None)
        if not value or (isinstance(value, (str, tuple)) and len(value) == 0):
            report.add_warning(f"Recommended field '{field_name}' is empty.")

    return report


def _detect_conflicts(
    entry: IntelligenceEntry, storage: IntelligenceStorage
) -> list[str]:
    """Detect entries that may contradict the given entry."""
    conflicts: list[str] = []
    existing = storage.retrieve_by_layer(entry.layer, limit=20)
    entry_lower = entry.content.lower() if entry.content else ""

    for existing_entry in existing:
        if existing_entry.id == entry.id:
            continue
        # Simple heuristic: same category + similar title but different content
        # could indicate a conflict.
        if existing_entry.category and existing_entry.category == entry.category:
            existing_words = set(re.findall(r"[a-z0-9_]{4,}", existing_entry.content.lower()))
            entry_words = set(re.findall(r"[a-z0-9_]{4,}", entry_lower))
            if existing_words and entry_words:
                overlap = len(existing_words.intersection(entry_words))
                total = len(existing_words.union(entry_words))
                similarity = overlap / max(total, 1)
                if 0.3 < similarity < 0.8:
                    conflicts.append(
                        f"Potential conflict with existing entry "
                        f"'{existing_entry.title}' (same layer/category, "
                        f"similar but not identical content)."
                    )
    return conflicts


def validate_for_publication(entry: IntelligenceEntry, storage: IntelligenceStorage) -> ValidationReport:
    """Stricter validation for entries about to be published to GitHub.

    Requires APPROVED status and passes all standard checks plus
    publication-specific rules.
    """
    report = validate_entry(entry, storage=storage, check_conflicts=True)

    # Publication requires APPROVED status
    if entry.status != "APPROVED":
        report.add_error(
            f"Entry must be APPROVED before publication. Current status: {entry.status}"
        )

    # Must have at least one tag for discoverability
    if not entry.tags or len(entry.tags) == 0:
        report.add_error("Publication requires at least one tag for discoverability.")

    # Must have a category
    if not entry.category or not entry.category.strip():
        report.add_error("Publication requires a non-empty category.")

    # Content must be substantive
    if entry.content and len(entry.content.strip()) < 50:
        report.add_error("Publication requires substantive content (at least 50 characters).")

    return report


def validate_for_proposal(entry: IntelligenceEntry) -> ValidationReport:
    """Lighter validation for new proposals (pre-approval)."""
    report = ValidationReport()

    for field_name in ("title", "layer", "summary", "content"):
        value = getattr(entry, field_name, None)
        if not value or (isinstance(value, str) and not value.strip()):
            report.add_error(f"Missing required field for proposal: {field_name}")

    if entry.layer and entry.layer not in INTELLIGENCE_LAYERS:
        report.add_error(f"Invalid layer: {entry.layer}")

    # Secret detection even at proposal stage
    combined_text = f"{entry.title} {entry.content}"
    for pattern in _SECRET_PATTERNS:
        if re.search(pattern, combined_text):
            report.add_error("Proposal contains what appears to be a secret or credential.")
            break

    return report
