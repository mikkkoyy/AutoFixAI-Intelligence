"""Intelligence Manager -- Central Service for Intelligence Lifecycle.

The IntelligenceManager orchestrates the full lifecycle of reusable AI
intelligence:

    Discovery -> Validation -> Proposal -> User Approval -> Storage
    -> Validation -> Publication (GitHub sync)

It is the bridge between:
    - Chat/AutoFix (intelligence consumers)
    - Intelligence Storage (local persistent store)
    - GitHub Intelligence Repository (remote shared store)
    - Project Runtime Memory (private, NEVER published)

CRITICAL SEPARATION:
    IntelligenceManager  handles reusable intelligence only.
    Project Memory (.autofix/memory/) remains completely separate.
    The manager NEVER modifies project memory.
    The manager NEVER silently publishes to GitHub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.agents.intelligence_store import (
    INTELLIGENCE_LAYERS,
    IntelligenceEntry,
    IntelligenceStorage,
    PendingQueue,
    STATUS_APPROVED,
    STATUS_DEPRECATED,
    STATUS_DISCOVERED,
    STATUS_PROPOSED,
    STATUS_PUBLISHED,
)
from app.agents.intelligence_sync import IntelligenceSync, SyncConfig, SyncResult
from app.agents.intelligence_validator import (
    ValidationReport,
    validate_entry,
    validate_for_proposal,
    validate_for_publication,
)
from app.agents.knowledge_security import sanitize_text


# -----------------------------------------------------------------------
# Intelligence Proposal (user-facing)
# -----------------------------------------------------------------------


@dataclass
class IntelligenceProposal:
    """A discovered intelligence candidate awaiting user approval."""

    entry: IntelligenceEntry
    validation_report: ValidationReport
    origin_context: str = ""  # safe context description (redacted)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.entry.id,
            "title": self.entry.title,
            "layer": self.entry.layer,
            "category": self.entry.category,
            "summary": self.entry.summary,
            "content": self.entry.content,
            "tags": list(self.entry.tags),
            "source": self.entry.source,
            "status": self.entry.status,
            "confidence": round(float(self.confidence), 2),
            "origin_context": self.origin_context,
            "validation_ok": self.validation_report.ok,
            "validation_errors": self.validation_report.errors,
            "validation_warnings": self.validation_report.warnings,
        }


# -----------------------------------------------------------------------
# Layer Selection Heuristics
# -----------------------------------------------------------------------

_INTENT_LAYER_MAP: dict[str, tuple[str, ...]] = {
    "greeting": ("behavior", "decision"),
    "conversation": ("behavior", "knowledge"),
    "question": ("knowledge", "reasoning"),
    "explanation": ("knowledge", "reasoning"),
    "analysis": ("reasoning", "decision"),
    "brainstorm": ("reasoning", "planning"),
    "recommendation": ("reasoning", "decision"),
    "coding_request": ("coding", "planning", "tools", "verification"),
    "debugging": ("reasoning", "coding", "recovery", "verification"),
    "project_request": ("planning", "coding", "agents", "tools", "verification"),
    "change_request": ("coding", "planning", "verification", "recovery"),
    "plan_request": ("planning", "reasoning", "decision"),
    "proposal_request": ("planning", "coding", "verification"),
}


def layers_for_intent(intent_category: str) -> tuple[str, ...]:
    """Map a conversation intent to relevant intelligence layers."""
    key = intent_category.lower().replace(" ", "_")
    return _INTENT_LAYER_MAP.get(key, ("knowledge", "behavior"))


# -----------------------------------------------------------------------
# Intelligence Manager
# -----------------------------------------------------------------------


class IntelligenceManager:
    """Central service for intelligence lifecycle management.

    Responsibilities:
        - Discover reusable intelligence from conversations/tasks
        - Validate intelligence entries
        - Manage the proposal/approval workflow
        - Store and retrieve intelligence
        - Synchronize with GitHub
        - Provide relevant intelligence for Chat/AutoFix context

    The manager is deterministic where possible and fault-tolerant
    everywhere -- failures are reported honestly, never silently swallowed.
    """

    def __init__(self, workspace: str):
        self._workspace = workspace
        self._storage = IntelligenceStorage(workspace)
        self._pending = PendingQueue(workspace)
        self._sync: IntelligenceSync | None = None

    @property
    def storage(self) -> IntelligenceStorage:
        return self._storage

    @property
    def pending_queue(self) -> PendingQueue:
        return self._pending

    def get_sync(self, config: SyncConfig | None = None) -> IntelligenceSync:
        """Lazy-initialized sync module."""
        if self._sync is None:
            self._sync = IntelligenceSync(self._storage, config or SyncConfig())
        return self._sync

    # -- Discovery -------------------------------------------------------

    def discover(
        self,
        title: str,
        layer: str,
        content: str,
        *,
        category: str = "",
        summary: str = "",
        tags: tuple[str, ...] = (),
        source: str = "",
        confidence: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> IntelligenceProposal:
        """Create an intelligence entry from a discovered insight.

        The entry is validated and placed in DISCOVERED status.
        No storage or network operations occur -- this is pure creation.
        """
        entry = IntelligenceEntry(
            title=title.strip(),
            layer=layer,
            category=category.strip(),
            summary=summary.strip() or _auto_summary(content),
            content=content.strip(),
            tags=tuple(t.strip().lower() for t in tags if t.strip()),
            source=source.strip(),
            status=STATUS_DISCOVERED,
            metadata=metadata or {},
        )
        report = validate_entry(entry, storage=self._storage, check_conflicts=False)
        return IntelligenceProposal(
            entry=entry,
            validation_report=report,
            origin_context=source,
            confidence=confidence,
        )

    def propose(self, proposal: IntelligenceProposal) -> dict[str, Any]:
        """Submit a discovered intelligence for user approval.

        Transitions the entry to PROPOSED and enqueues it in the pending
        queue. Returns a result dict for the UI.
        """
        entry = proposal.entry
        if not proposal.validation_report.ok:
            return {
                "ok": False,
                "message": "Intelligence failed validation.",
                "errors": proposal.validation_report.errors,
            }
        entry.transition(STATUS_PROPOSED)
        path = self._pending.enqueue(entry)
        if not path:
            entry.status = STATUS_DISCOVERED
            return {
                "ok": False,
                "message": "Failed to queue intelligence proposal.",
            }
        return {
            "ok": True,
            "entry_id": entry.id,
            "message": f"Intelligence proposal '{entry.title}' is awaiting your approval.",
        }

    # -- Approval --------------------------------------------------------

    def approve(self, entry_id: str) -> dict[str, Any]:
        """Approve a pending intelligence entry.

        The entry is dequeued, transitioned to APPROVED, and stored.
        """
        entry = self._pending.dequeue(entry_id)
        if entry is None:
            return {
                "ok": False,
                "message": f"No pending entry with ID {entry_id}.",
            }
        entry.transition(STATUS_APPROVED)
        stored = self._storage.store(entry)
        if not stored:
            return {
                "ok": False,
                "message": "Failed to persist approved intelligence.",
            }
        return {
            "ok": True,
            "entry_id": entry.id,
            "message": f"Intelligence '{entry.title}' approved and stored locally.",
        }

    def reject(self, entry_id: str) -> dict[str, Any]:
        """Reject and discard a pending intelligence entry."""
        entry = self._pending.dequeue(entry_id)
        if entry is None:
            return {
                "ok": False,
                "message": f"No pending entry with ID {entry_id}.",
            }
        return {
            "ok": True,
            "message": f"Intelligence proposal '{entry.title}' discarded.",
        }

    # -- Storage & Retrieval ---------------------------------------------

    def store(self, entry: IntelligenceEntry) -> bool:
        """Directly store an entry (e.g., after external approval)."""
        return self._storage.store(entry)

    def load(self, entry_id: str) -> IntelligenceEntry | None:
        return self._storage.load(entry_id)

    def retrieve_relevant(
        self,
        query: str,
        intent_category: str = "",
        limit: int = 10,
    ) -> list[IntelligenceEntry]:
        """Retrieve intelligence relevant to the current context.

        Uses intent category to select appropriate layers, then searches
        for relevant entries.
        """
        layers = layers_for_intent(intent_category) if intent_category else None
        return self._storage.retrieve_relevant(query, layers=layers, limit=limit)

    def search(self, query: str, limit: int = 10) -> list[IntelligenceEntry]:
        return self._storage.search(query, limit=limit)

    def list_entries(self, **kwargs) -> list[IntelligenceEntry]:
        return self._storage.list_entries(**kwargs)

    # -- Context Building ------------------------------------------------

    def build_intelligence_context(
        self,
        query: str,
        intent_category: str = "",
        max_chars: int = 2000,
    ) -> str:
        """Build a compact intelligence context block for AI prompts.

        Returns a formatted string with relevant intelligence that can be
        injected into Chat or AutoFix system prompts. Empty string if no
        relevant intelligence is found.
        """
        entries = self.retrieve_relevant(query, intent_category, limit=5)
        if not entries:
            return ""

        lines: list[str] = []
        budget = max_chars

        header = "Relevant AI intelligence:\n"
        lines.append(header)
        budget -= len(header)

        for entry in entries:
            layer_tag = f"[{entry.layer.upper()}]"
            snippet = " ".join((entry.summary or entry.content).split())[:300]
            line = f"- {layer_tag} {entry.title}: {snippet}\n"
            if len(line) > budget:
                break
            lines.append(line)
            budget -= len(line)

        return "".join(lines) if len(lines) > 1 else ""

    # -- GitHub Synchronization ------------------------------------------

    def sync_push(self, config: SyncConfig | None = None) -> SyncResult:
        """Push approved entries to GitHub."""
        sync = self.get_sync(config)
        return sync.push_approved()

    def sync_pull(self, config: SyncConfig | None = None) -> SyncResult:
        """Pull intelligence entries from GitHub."""
        sync = self.get_sync(config)
        return sync.pull_remote()

    # -- Deprecation & Cleanup -------------------------------------------

    def deprecate(self, entry_id: str) -> dict[str, Any]:
        """Deprecate an intelligence entry."""
        entry = self._storage.load(entry_id)
        if entry is None:
            return {"ok": False, "message": f"Entry {entry_id} not found."}
        entry.transition(STATUS_DEPRECATED)
        self._storage.store(entry)
        return {"ok": True, "message": f"Entry '{entry.title}' deprecated."}

    def delete(self, entry_id: str) -> dict[str, Any]:
        return self._storage.delete(entry_id)

    # -- Audit & Stats ---------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return self._storage.stats()

    def audit(self) -> dict[str, Any]:
        storage_stats = self._storage.stats()
        pending = self._pending.list_pending()
        sync_audit = {}
        if self._sync:
            sync_audit = self._sync.audit()
        return {
            "storage": storage_stats,
            "pending_count": len(pending),
            "pending_entries": [
                {"id": e.id, "title": e.title, "layer": e.layer}
                for e in pending[:20]
            ],
            "sync": sync_audit,
        }


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _auto_summary(content: str) -> str:
    """Generate a one-line summary from content."""
    text = " ".join(content.split())
    if len(text) <= 150:
        return text
    return text[:147] + "..."
