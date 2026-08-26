"""Persistent intelligence store for AIRA."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class IntelligenceStore:
    """Stores durable AIRA knowledge as JSON records."""

    def __init__(self, root: str | Path = "AIRA/intelligence"):
        self.root = Path(root)
        self.memory_dir = self.root / "memory"
        self.knowledge_dir = self.root / "knowledge"
        self.upgrades_dir = self.root / "upgrades"

        for directory in (
            self.memory_dir,
            self.knowledge_dir,
            self.upgrades_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def save_memory(
        self,
        key: str,
        content: Any,
        category: str = "general",
        importance: float = 0.5,
    ) -> Path:
        if not key.strip():
            raise ValueError("Memory key cannot be empty")

        importance = max(0.0, min(1.0, float(importance)))

        record = {
            "key": key,
            "category": category,
            "content": content,
            "importance": importance,
            "updated_at": self._timestamp(),
        }

        path = self.memory_dir / f"{self._safe_name(key)}.json"
        self._write_json(path, record)
        return path

    def load_memory(self, key: str) -> dict[str, Any] | None:
        path = self.memory_dir / f"{self._safe_name(key)}.json"

        if not path.exists():
            return None

        return json.loads(path.read_text(encoding="utf-8"))

    def save_knowledge(
        self,
        topic: str,
        content: Any,
        source: str = "internal",
    ) -> Path:
        if not topic.strip():
            raise ValueError("Knowledge topic cannot be empty")

        record = {
            "topic": topic,
            "content": content,
            "source": source,
            "updated_at": self._timestamp(),
        }

        path = self.knowledge_dir / f"{self._safe_name(topic)}.json"
        self._write_json(path, record)
        return path

    def save_upgrade_proposal(
        self,
        title: str,
        description: str,
        files: list[str] | None = None,
    ) -> Path:
        if not title.strip():
            raise ValueError("Upgrade title cannot be empty")

        record = {
            "title": title,
            "description": description,
            "files": files or [],
            "status": "proposed",
            "created_at": self._timestamp(),
        }

        filename = self._safe_name(title)
        path = self.upgrades_dir / f"{filename}.json"
        self._write_json(path, record)
        return path

    @staticmethod
    def _safe_name(value: str) -> str:
        import re

        name = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
        name = name.strip("._")

        if not name:
            raise ValueError("Invalid storage name")

        return name[:120]

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)
