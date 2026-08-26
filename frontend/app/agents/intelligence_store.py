"""Intelligence Entry Model and Persistent Storage.

Intelligence Storage is a structured, persistent knowledge/intelligence store
that is SEPARATE from project runtime memory.  It is designed for reusable,
validated, approved intelligence that can be synchronized with the GitHub AI
Intelligence repository (mikkkoyy/AutoFixAI-Intelligence).

Local storage layout::

    <workspace>\\.autofix\\intelligence\\
        index.json              -- master index of all entries
        behavior/               -- per-layer JSON files
        reasoning/
        planning/
        knowledge/
        coding/
        agents/
        tools/
        verification/
        recovery/
        decision/
        pending/                -- entries awaiting approval
        cache/                  -- synced from GitHub for offline use

CRITICAL SEPARATION RULES:
    Intelligence Storage  !=  Project Runtime Memory (.autofix/memory/)
    Intelligence Storage  =  reusable, validated, approved, version-controlled
    Project Memory        =  private, project-specific, local, runtime-oriented

Never publish private project memory to the intelligence repository.
Never store secrets in the intelligence repository.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------
# Intelligence Framework Layers
# -----------------------------------------------------------------------

INTELLIGENCE_LAYERS = (
    "behavior",
    "reasoning",
    "planning",
    "knowledge",
    "coding",
    "agents",
    "tools",
    "verification",
    "recovery",
    "decision",
)

# -----------------------------------------------------------------------
# Entry Status Lifecycle
# -----------------------------------------------------------------------

STATUS_DISCOVERED = "DISCOVERED"
STATUS_PROPOSED = "PROPOSED"
STATUS_APPROVED = "APPROVED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_DEPRECATED = "DEPRECATED"

VALID_STATUSES = (
    STATUS_DISCOVERED,
    STATUS_PROPOSED,
    STATUS_APPROVED,
    STATUS_PUBLISHED,
    STATUS_DEPRECATED,
)

# Valid transitions: from_status -> set of allowed to_statuses
_STATUS_TRANSITIONS: dict[str, set[str]] = {
    STATUS_DISCOVERED: {STATUS_PROPOSED, STATUS_DEPRECATED},
    STATUS_PROPOSED: {STATUS_APPROVED, STATUS_DEPRECATED, STATUS_DISCOVERED},
    STATUS_APPROVED: {STATUS_PUBLISHED, STATUS_DEPRECATED},
    STATUS_PUBLISHED: {STATUS_DEPRECATED},
    STATUS_DEPRECATED: set(),
}

# -----------------------------------------------------------------------
# Storage Directories
# -----------------------------------------------------------------------

_INTELLIGENCE_DIR = ".autofix/intelligence"
_INDEX_FILE = "index.json"
_PENDING_DIR = "pending"
_CACHE_DIR = "cache"

# -----------------------------------------------------------------------
# Entry Model
# -----------------------------------------------------------------------


@dataclass
class IntelligenceEntry:
    """A single structured intelligence record.

    Fields:
        id              Unique identifier (UUID4)
        title           Human-readable title
        layer           Intelligence Framework layer (one of INTELLIGENCE_LAYERS)
        category        Free-form category within the layer
        summary         One-paragraph summary
        content         Full markdown content
        tags            Searchable tags
        source          Origin description (redacted, no secrets)
        status          Lifecycle status (see VALID_STATUSES)
        version         Integer version (incremented on modification)
        created_at      ISO-8601 creation timestamp
        updated_at      ISO-8601 last-modification timestamp
        related_entries List of related entry IDs
        metadata        Extensible key-value metadata
    """

    id: str = ""
    title: str = ""
    layer: str = ""
    category: str = ""
    summary: str = ""
    content: str = ""
    tags: tuple[str, ...] = ()
    source: str = ""
    status: str = STATUS_DISCOVERED
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    related_entries: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = _new_id()
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tags"] = list(self.tags)
        d["related_entries"] = list(self.related_entries)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntelligenceEntry:
        known = set(cls.__dataclass_fields__)
        payload = {}
        for k, v in (data or {}).items():
            if k in known:
                if k in ("tags", "related_entries") and isinstance(v, list):
                    payload[k] = tuple(v)
                elif k == "metadata" and not isinstance(v, dict):
                    payload[k] = {}
                else:
                    payload[k] = v
        return cls(**payload)

    def transition(self, new_status: str) -> bool:
        """Attempt a status transition. Returns True if valid."""
        allowed = _STATUS_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            return False
        self.status = new_status
        self.updated_at = _now_iso()
        self.version += 1
        return True

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in _STATUS_TRANSITIONS.get(self.status, set())

    def is_permanent(self) -> bool:
        return self.status in (STATUS_APPROVED, STATUS_PUBLISHED)

    def keywords(self) -> set[str]:
        """Return all searchable keywords from the entry."""
        text = f"{self.title} {self.category} {self.summary} {' '.join(self.tags)}".lower()
        return {w for w in re.findall(r"[a-z0-9_]{3,}", text)}


def _new_id() -> str:
    return str(uuid.uuid4())[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------
# Persistent Storage
# -----------------------------------------------------------------------


class IntelligenceStorage:
    """Persistent local storage for intelligence entries.

    The storage layer manages the index, per-layer files, pending queue,
    and cache directory.  All operations are deterministic, fault-tolerant,
    and never raise for operational failures.
    """

    def __init__(self, workspace: str | Path):
        self._workspace = Path(workspace)
        self._base = self._workspace / _INTELLIGENCE_DIR
        self._index_path = self._base / _INDEX_FILE
        self._index: dict[str, dict] | None = None

    @property
    def base_dir(self) -> Path:
        return self._base

    def _ensure_dirs(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        for layer in INTELLIGENCE_LAYERS:
            (self._base / layer).mkdir(exist_ok=True)
        (self._base / _PENDING_DIR).mkdir(exist_ok=True)
        (self._base / _CACHE_DIR).mkdir(exist_ok=True)

    # -- Index operations ------------------------------------------------

    def _load_index(self) -> dict[str, dict]:
        if self._index is not None:
            return self._index
        self._ensure_dirs()
        if self._index_path.exists():
            try:
                raw = json.loads(self._index_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._index = raw
                    return self._index
            except (json.JSONDecodeError, OSError):
                pass
        self._index = {}
        return self._index

    def _save_index(self) -> None:
        self._ensure_dirs()
        index = self._load_index()
        try:
            self._index_path.write_text(
                json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _entry_layer_path(self, entry: IntelligenceEntry) -> Path:
        layer = entry.layer if entry.layer in INTELLIGENCE_LAYERS else "knowledge"
        return self._base / layer / f"{entry.id}.json"

    # -- Public API ------------------------------------------------------

    def store(self, entry: IntelligenceEntry) -> bool:
        """Persist an entry to its layer directory and update the index."""
        self._ensure_dirs()
        index = self._load_index()
        index[entry.id] = {
            "id": entry.id,
            "title": entry.title,
            "layer": entry.layer,
            "category": entry.category,
            "status": entry.status,
            "version": entry.version,
            "tags": list(entry.tags),
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
        }
        path = self._entry_layer_path(entry)
        try:
            path.write_text(
                json.dumps(entry.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return False
        self._save_index()
        return True

    def load(self, entry_id: str) -> IntelligenceEntry | None:
        """Load a single entry by ID from the index."""
        index = self._load_index()
        meta = index.get(entry_id)
        if meta is None:
            return None
        layer = meta.get("layer", "knowledge")
        path = self._base / layer / f"{entry_id}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return IntelligenceEntry.from_dict(raw)
        except (json.JSONDecodeError, OSError):
            return None

    def update(self, entry: IntelligenceEntry) -> bool:
        """Update an existing entry (increments version)."""
        entry.updated_at = _now_iso()
        entry.version += 1
        return self.store(entry)

    def delete(self, entry_id: str) -> bool:
        """Remove an entry from storage and index."""
        index = self._load_index()
        meta = index.pop(entry_id, None)
        if meta is None:
            return False
        layer = meta.get("layer", "knowledge")
        path = self._base / layer / f"{entry_id}.json"
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        self._save_index()
        return True

    def list_entries(
        self,
        layer: str | None = None,
        status: str | None = None,
        tags: set[str] | None = None,
    ) -> list[IntelligenceEntry]:
        """List entries filtered by layer, status, and/or tags."""
        index = self._load_index()
        entries: list[IntelligenceEntry] = []
        for meta in index.values():
            if layer and meta.get("layer") != layer:
                continue
            if status and meta.get("status") != status:
                continue
            if tags:
                entry_tags = set(meta.get("tags", []))
                if not tags.intersection(entry_tags):
                    continue
            entry = self.load(meta["id"])
            if entry is not None:
                entries.append(entry)
        return entries

    def search(self, query: str, limit: int = 10) -> list[IntelligenceEntry]:
        """Keyword search across all entries, scored by relevance."""
        index = self._load_index()
        query_words = set(w.lower() for w in re.findall(r"[a-z0-9_]{3,}", query.lower()))
        if not query_words:
            return []
        scored: list[tuple[int, str]] = []
        for entry_id, meta in index.items():
            meta_text = f"{meta.get('title', '')} {meta.get('category', '')} {' '.join(meta.get('tags', []))}".lower()
            meta_words = set(re.findall(r"[a-z0-9_]{3,}", meta_text))
            overlap = len(query_words.intersection(meta_words))
            if overlap > 0:
                scored.append((-overlap, entry_id))
        scored.sort()
        results: list[IntelligenceEntry] = []
        for _, entry_id in scored[:limit]:
            entry = self.load(entry_id)
            if entry is not None:
                results.append(entry)
        return results

    def retrieve_by_layer(self, layer: str, query: str = "", limit: int = 10) -> list[IntelligenceEntry]:
        """Retrieve entries from a specific layer, optionally filtered by query."""
        if query:
            all_entries = self.search(query, limit=50)
            return [e for e in all_entries if e.layer == layer][:limit]
        return self.list_entries(layer=layer)[:limit]

    def retrieve_relevant(
        self,
        query: str,
        layers: tuple[str, ...] | None = None,
        limit: int = 10,
    ) -> list[IntelligenceEntry]:
        """Retrieve relevant intelligence for the current task/context.

        Searches across specified layers (or all if None), returns the most
        relevant entries up to the limit.
        """
        index = self._load_index()
        query_words = set(w.lower() for w in re.findall(r"[a-z0-9_]{3,}", query.lower()))
        if not query_words:
            return []

        scored: list[tuple[int, int, str]] = []
        for idx, (entry_id, meta) in enumerate(index.items()):
            if layers and meta.get("layer") not in layers:
                continue
            meta_text = (
                f"{meta.get('title', '')} {meta.get('category', '')} "
                f"{' '.join(meta.get('tags', []))}"
            ).lower()
            meta_words = set(re.findall(r"[a-z0-9_]{3,}", meta_text))
            overlap = len(query_words.intersection(meta_words))
            if overlap > 0:
                scored.append((-overlap, idx, entry_id))
        scored.sort()

        results: list[IntelligenceEntry] = []
        for _, _, entry_id in scored[:limit]:
            entry = self.load(entry_id)
            if entry is not None:
                results.append(entry)
        return results

    def count(self, layer: str | None = None, status: str | None = None) -> int:
        index = self._load_index()
        count = 0
        for meta in index.values():
            if layer and meta.get("layer") != layer:
                continue
            if status and meta.get("status") != status:
                continue
            count += 1
        return count

    def entry_exists(self, title: str, layer: str) -> bool:
        """Check for duplicate entries by title + layer."""
        index = self._load_index()
        normalized = _normalize_title(title)
        for meta in index.values():
            if meta.get("layer") == layer and _normalize_title(meta.get("title", "")) == normalized:
                return True
        return False

    def clear(self) -> int:
        """Remove ALL entries. Returns count of entries removed."""
        index = self._load_index()
        count = len(index)
        for entry_id in list(index.keys()):
            self.delete(entry_id)
        return count

    def stats(self) -> dict[str, Any]:
        """Storage statistics."""
        index = self._load_index()
        by_layer: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for meta in index.values():
            layer = meta.get("layer", "unknown")
            status = meta.get("status", "unknown")
            by_layer[layer] = by_layer.get(layer, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total": len(index),
            "by_layer": by_layer,
            "by_status": by_status,
        }


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


# -----------------------------------------------------------------------
# Pending Queue (for approval workflow)
# -----------------------------------------------------------------------


class PendingQueue:
    """Queue of intelligence entries awaiting user approval.

    Pending entries are stored as JSON files in the pending/ directory.
    """

    def __init__(self, workspace: str | Path):
        self._workspace = Path(workspace)
        self._pending_dir = self._workspace / _INTELLIGENCE_DIR / _PENDING_DIR

    def _ensure_dir(self) -> None:
        self._pending_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(self, entry: IntelligenceEntry) -> str:
        """Add an entry to the pending queue. Returns the file path."""
        self._ensure_dir()
        filename = f"{entry.id}.json"
        path = self._pending_dir / filename
        payload = entry.to_dict()
        payload["_queued_at"] = _now_iso()
        try:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    def dequeue(self, entry_id: str) -> IntelligenceEntry | None:
        """Remove and return an entry from the pending queue."""
        path = self._pending_dir / f"{entry_id}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = IntelligenceEntry.from_dict(raw)
            path.unlink()
            return entry
        except (json.JSONDecodeError, OSError):
            return None

    def peek(self, entry_id: str) -> IntelligenceEntry | None:
        """Read an entry from the pending queue without removing it."""
        path = self._pending_dir / f"{entry_id}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return IntelligenceEntry.from_dict(raw)
        except (json.JSONDecodeError, OSError):
            return None

    def list_pending(self) -> list[IntelligenceEntry]:
        """Return all pending entries."""
        self._ensure_dir()
        entries: list[IntelligenceEntry] = []
        for p in sorted(self._pending_dir.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                entries.append(IntelligenceEntry.from_dict(raw))
            except (json.JSONDecodeError, OSError):
                continue
        return entries

    def count(self) -> int:
        self._ensure_dir()
        return sum(1 for _ in self._pending_dir.glob("*.json"))

    def clear(self) -> int:
        """Remove all pending entries. Returns count."""
        count = 0
        for p in self._pending_dir.glob("*.json"):
            try:
                p.unlink()
                count += 1
            except OSError:
                pass
        return count
