"""GitHub Intelligence Synchronization.

Handles bidirectional synchronization between local Intelligence Storage
and the GitHub AI Intelligence repository (mikkkoyy/AutoFixAI-Intelligence).

Architecture:
    Local Storage  <-->  IntelligenceSync  <-->  GitHub Repository

CRITICAL RULES:
    - Never silently publish without user approval
    - GitHub failure does NOT destroy local approved state
    - Only report sync success after the actual operation succeeds
    - Never publish secrets, project memory, or runtime data
    - The GitHub repo stores reusable intelligence, not secret stores

GitHub repository layout::

    .autofix-ai/
        behavior/
        reasoning/
        planning/
        knowledge/
        coding/
        agents/
        tools/
        verification/
        recovery/
        decision/
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.intelligence_store import (
    INTELLIGENCE_LAYERS,
    IntelligenceEntry,
    IntelligenceStorage,
    STATUS_APPROVED,
    STATUS_PUBLISHED,
    _now_iso,
)
from app.agents.intelligence_validator import validate_for_publication

# -----------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------

DEFAULT_GITHUB_REPO = "mikkkoyy/AutoFixAI-Intelligence"
DEFAULT_API_URL = "https://api.github.com"
DEFAULT_BRANCH = "main"
DEFAULT_DIRNAME = ".autofix-ai"
REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class SyncConfig:
    """Configuration for GitHub intelligence synchronization."""

    repo: str = DEFAULT_GITHUB_REPO
    token: str = ""
    api_url: str = DEFAULT_API_URL
    branch: str = DEFAULT_BRANCH
    dirname: str = DEFAULT_DIRNAME

    @classmethod
    def from_env(cls, env=None) -> SyncConfig | None:
        """Create from environment variables. Returns None if not configured."""
        env = env or os.environ

        def get(key: str) -> str:
            return str(env.get(key, "")).strip()

        token = get("AUTOFIX_INTELLIGENCE_TOKEN") or get("GITHUB_TOKEN")
        repo = get("AUTOFIX_INTELLIGENCE_REPO") or DEFAULT_GITHUB_REPO
        if not token:
            return None
        return cls(
            repo=repo,
            token=token,
            api_url=get("AUTOFIX_INTELLIGENCE_API_URL") or DEFAULT_API_URL,
            branch=get("AUTOFIX_INTELLIGENCE_BRANCH") or DEFAULT_BRANCH,
            dirname=get("AUTOFIX_INTELLIGENCE_DIR") or DEFAULT_DIRNAME,
        )

    def is_configured(self) -> bool:
        return bool(self.repo and self.token)


# -----------------------------------------------------------------------
# Sync Result
# -----------------------------------------------------------------------


@dataclass
class SyncResult:
    """Outcome of a sync operation."""

    ok: bool = False
    pushed: int = 0
    pulled: int = 0
    message: str = ""
    errors: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False


# -----------------------------------------------------------------------
# GitHub API Client (stdlib only)
# -----------------------------------------------------------------------


class IntelligenceGitHubClient:
    """Minimal GitHub REST v3 client for intelligence sync.

    Injected dependency for tests (no real network calls).
    """

    def __init__(self, config: SyncConfig):
        self.config = config

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.config.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        headers.update(extra or {})
        return headers

    def _request(self, method: str, url: str, payload=None):
        headers = self._headers()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, body
        except urllib.error.HTTPError as exc:
            raise SyncError(f"GitHub HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SyncError(f"GitHub unreachable: {exc}") from exc

    def get_json(self, path: str) -> Any:
        url = self.config.api_url.rstrip("/") + path
        _status, body = self._request("GET", url)
        return json.loads(body)

    def put_contents(
        self, path: str, content_text: str, message: str, sha: str | None = None
    ) -> dict:
        from urllib.parse import quote

        owner, name = self.config.repo.split("/", 1)
        url = (
            self.config.api_url.rstrip("/")
            + "/repos/" + quote(owner, safe="") + "/" + quote(name, safe="")
            + "/contents/" + quote(path, safe="/")
        )
        import base64

        payload = {
            "message": message,
            "content": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
            "branch": self.config.branch,
        }
        if sha:
            payload["sha"] = sha
        _status, body = self._request("PUT", url, payload=payload)
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        return {
            "commit": ((data.get("commit") or {}).get("sha") or "")[:12],
            "html_url": (data.get("content") or {}).get("html_url") or "",
        }

    def file_sha(self, path: str) -> str | None:
        from urllib.parse import quote

        owner, name = self.config.repo.split("/", 1)
        url = (
            self.config.api_url.rstrip("/")
            + "/repos/" + quote(owner, safe="") + "/" + quote(name, safe="")
            + "/contents/" + quote(path, safe="/")
            + f"?ref={quote(self.config.branch, safe='')}"
        )
        try:
            _status, body = self._request("GET", url)
            return json.loads(body).get("sha")
        except Exception:
            return None

    def list_tree(self) -> list[str]:
        from urllib.parse import quote

        owner, name = self.config.repo.split("/", 1)
        url = (
            self.config.api_url.rstrip("/")
            + "/repos/" + quote(owner, safe="") + "/" + quote(name, safe="")
            + f"/git/trees/{quote(self.config.branch, safe='')}?recursive=1"
        )
        data = self.get_json(url)
        prefix = self.config.dirname.strip("/") + "/"
        return [
            item["path"]
            for item in data.get("tree", [])
            if item.get("type") == "blob"
            and str(item.get("path", "")).startswith(prefix)
            and str(item.get("path", "")).endswith(".json")
        ]

    def fetch_file(self, path: str) -> str:
        from urllib.parse import quote

        owner, name = self.config.repo.split("/", 1)
        url = (
            self.config.api_url.rstrip("/")
            + "/repos/" + quote(owner, safe="") + "/" + quote(name, safe="")
            + "/contents/" + quote(path, safe="/")
            + f"?ref={quote(self.config.branch, safe='')}"
        )
        _status, body = self._request(
            "GET", url, extra={"Accept": "application/vnd.github.raw"}
        )
        return body


class SyncError(Exception):
    """Raised when a GitHub operation fails."""


# -----------------------------------------------------------------------
# Sync Operations
# -----------------------------------------------------------------------


class IntelligenceSync:
    """Bidirectional sync between local storage and GitHub.

    Key behaviors:
        - push() only pushes APPROVED entries that pass publication validation
        - pull() fetches remote entries and caches locally
        - GitHub failure does NOT destroy local state
        - Pending queue survives sync failures
    """

    def __init__(
        self,
        storage: IntelligenceStorage,
        config: SyncConfig | None = None,
        client: IntelligenceGitHubClient | None = None,
    ):
        self.storage = storage
        self.config = config or SyncConfig()
        self._client = client

    @property
    def client(self) -> IntelligenceGitHubClient:
        if self._client is None:
            self._client = IntelligenceGitHubClient(self.config)
        return self._client

    def is_configured(self) -> bool:
        return self.config.is_configured()

    # -- Push ------------------------------------------------------------

    def push_approved(self) -> SyncResult:
        """Push all APPROVED entries to GitHub.

        Only pushes entries that pass validation. Each entry is committed
        individually so partial failures don't block other entries.
        """
        result = SyncResult()
        if not self.is_configured():
            result.add_error("GitHub intelligence sync is not configured.")
            return result

        approved = self.storage.list_entries(status=STATUS_APPROVED)
        if not approved:
            result.ok = True
            result.message = "No approved entries to push."
            return result

        for entry in approved:
            try:
                report = validate_for_publication(entry, self.storage)
                if not report.ok:
                    result.add_error(
                        f"Entry '{entry.title}' failed validation: "
                        + "; ".join(report.errors)
                    )
                    continue
                self._push_entry(entry, result)
            except SyncError as exc:
                result.add_error(f"Failed to push '{entry.title}': {exc}")

        if result.pushed > 0 and not result.errors:
            result.ok = True
            result.message = f"Successfully pushed {result.pushed} entries to GitHub."
        elif result.pushed > 0:
            result.message = f"Pushed {result.pushed} entries with {len(result.errors)} errors."
        else:
            result.message = f"No entries pushed. Errors: {'; '.join(result.errors)}"

        return result

    def _push_entry(self, entry: IntelligenceEntry, result: SyncResult) -> None:
        """Push a single entry to GitHub."""
        layer = entry.layer if entry.layer in INTELLIGENCE_LAYERS else "knowledge"
        path = f"{self.config.dirname.strip('/')}/{layer}/{entry.id}.json"
        content = json.dumps(entry.to_dict(), indent=2, ensure_ascii=False)

        sha = self.client.file_sha(path)
        commit_msg = f"Intelligence: {entry.title} (v{entry.version})"
        self.client.put_contents(path, content, commit_msg, sha)

        # Mark as PUBLISHED after successful push
        entry.transition(STATUS_PUBLISHED)
        self.storage.store(entry)

        result.pushed += 1

    # -- Pull ------------------------------------------------------------

    def pull_remote(self) -> SyncResult:
        """Pull intelligence entries from GitHub to local cache.

        Fetched entries are stored in the cache directory and the local
        index. Existing local entries are not overwritten unless the
        remote version is newer.
        """
        result = SyncResult()
        if not self.is_configured():
            result.add_error("GitHub intelligence sync is not configured.")
            return result

        try:
            tree_paths = self.client.list_tree()
        except SyncError as exc:
            result.add_error(f"Failed to list remote tree: {exc}")
            return result

        for remote_path in tree_paths:
            try:
                self._pull_entry(remote_path, result)
            except SyncError as exc:
                result.add_error(f"Failed to pull {remote_path}: {exc}")

        if result.pulled > 0:
            result.ok = True
            result.message = f"Successfully pulled {result.pulled} entries from GitHub."
        else:
            result.ok = len(result.errors) == 0
            result.message = "No new entries pulled."

        return result

    def _pull_entry(self, remote_path: str, result: SyncResult) -> None:
        """Pull a single entry from GitHub."""
        content_text = self.client.fetch_file(remote_path)
        remote_entry = IntelligenceEntry.from_dict(json.loads(content_text))

        # Check if local version exists and is newer
        local = self.storage.load(remote_entry.id)
        if local and local.version >= remote_entry.version:
            return  # local is up-to-date

        # Store the pulled entry
        self.storage.store(remote_entry)
        result.pulled += 1

    # -- Local state protection -----------------------------------------

    def save_offline_approved(self, entry: IntelligenceEntry) -> bool:
        """Save an approved entry locally when GitHub is unavailable.

        The entry is saved as APPROVED locally. When connectivity returns,
        push_approved() will pick it up automatically.
        """
        if entry.status != STATUS_APPROVED:
            entry.status = STATUS_APPROVED
        return self.storage.store(entry)

    def pending_push_queue(self) -> list[IntelligenceEntry]:
        """Return all locally APPROVED entries not yet PUBLISHED."""
        approved = self.storage.list_entries(status=STATUS_APPROVED)
        return [e for e in approved if e.status == STATUS_APPROVED]

    # -- Audit -----------------------------------------------------------

    def audit(self) -> dict[str, Any]:
        """Audit the sync state."""
        stats = self.storage.stats()
        pending_approved = self.pending_push_queue()
        return {
            "configured": self.is_configured(),
            "repo": self.config.repo if self.is_configured() else "",
            "storage_stats": stats,
            "pending_push": len(pending_approved),
            "pending_entries": [
                {"id": e.id, "title": e.title, "layer": e.layer}
                for e in pending_approved[:20]
            ],
        }
