import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional
from enum import Enum


class PermissionLevel(Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"


class KnowledgeState(Enum):
    UNVERIFIED = "unverified"
    TESTING = "testing"
    VERIFIED = "verified"
    FAILED = "failed"
    DEPRECATED = "deprecated"


class AutonomyLevel(Enum):
    AUTO = "auto"
    SAFE = "safe"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


def generate_id() -> str:
    return secrets.token_hex(16)


def generate_short_id() -> str:
    return secrets.token_hex(8)


def timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
