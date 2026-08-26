"""Shared AI knowledge — configurable GitHub repository integration.

STRICT SEPARATION (locked architecture):

    PROJECT MEMORY  →  <workspace>\\.autofix\\memory\\          (private)
    TASKS           →  <workspace>\\.autofix\\tasks\\           (private)
    SHARED KNOWLEDGE→  configured GitHub AI knowledge repo     (public/shared)

Project memory is NEVER published automatically; shared knowledge is NEVER
written without the user's explicit approval.  This module is storage and
retrieval plumbing only — it can never trigger a save on its own.

Repository layout (default root ``ai-knowledge``)::

    ai-knowledge/
      behavior/    interaction & response behavior patterns
      planning/    reusable planning strategies
      reasoning/   reasoning / debugging / decision strategies
      patterns/    reusable engineering patterns
      lessons/     validated lessons learned
      README.md

Configuration (environment):

    AUTOFIX_KNOWLEDGE_REPO     "owner/repo" (or full GitHub URL)   [required]
    GITHUB_TOKEN               personal access token                [or]
    AUTOFIX_KNOWLEDGE_TOKEN    knowledge-specific token override
    AUTOFIX_KNOWLEDGE_API_URL  default https://api.github.com
    AUTOFIX_KNOWLEDGE_REF      default branch, default "main"
    AUTOFIX_KNOWLEDGE_DIR      default "ai-knowledge"

All network operations are dependency-injectable (``client=``) so tests run
without credentials or connectivity.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from app.agents.knowledge_security import vet_knowledge

KNOWLEDGE_CATEGORIES = ("behavior", "planning", "reasoning", "patterns", "lessons")

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_REF = "main"
DEFAULT_DIRNAME = "ai-knowledge"
REQUEST_TIMEOUT_SECONDS = 20

#: Relative directory under workspace root for local knowledge persistence.
_LOCAL_KNOWLEDGE_DIR = ".autofix/knowledge"
_PENDING_DIR = "pending"
_CACHE_DIR = "cache"

#: Priority sentence injected wherever shared guidance is used.  Shared
#: knowledge is the FOURTH priority — it must never override project state.
PRIORITY_RULE = (
    "Shared AI knowledge is GUIDANCE ONLY. Priority when guidance conflicts "
    "with reality: (1) current project files, (2) current project "
    "configuration, (3) project-specific .autofix/memory, (4) this shared "
    "knowledge, (5) generic defaults. Never let it contradict actual project "
    "state."
)


class KnowledgeError(Exception):
    """Raised with a CLEAN, credential-free message when GitHub ops fail."""


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeConfig:
    repo: str            # "owner/name"
    token: str = ""
    api_url: str = DEFAULT_API_URL
    ref: str = DEFAULT_REF
    dirname: str = DEFAULT_DIRNAME

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0] if "/" in self.repo else self.repo

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1] if "/" in self.repo else ""

    @classmethod
    def from_env(cls, env=None) -> "KnowledgeConfig | None":
        import os

        env = os.environ if env is None else env

        def get(key: str) -> str:
            return str(env.get(key, "") or "").strip()

        raw_repo = get("AUTOFIX_KNOWLEDGE_REPO")
        if not raw_repo:
            return None
        # Accept full URLs like https://github.com/owner/repo(.git)
        match = re.search(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$",
                          raw_repo)
        repo = match.group(1) if match else raw_repo.strip("/").removesuffix(".git")
        if "/" not in repo:
            return None
        token = get("AUTOFIX_KNOWLEDGE_TOKEN") or get("GITHUB_TOKEN")
        if not token:
            return None
        return cls(
            repo=repo,
            token=token,
            api_url=get("AUTOFIX_KNOWLEDGE_API_URL") or DEFAULT_API_URL,
            ref=get("AUTOFIX_KNOWLEDGE_REF") or DEFAULT_REF,
            dirname=get("AUTOFIX_KNOWLEDGE_DIR") or DEFAULT_DIRNAME,
        )

    def is_configured(self) -> bool:
        return bool(self.repo and self.token)


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


@dataclass
class KnowledgeItem:
    """One approved-for-saving knowledge candidate."""

    category: str                 # one of KNOWLEDGE_CATEGORIES
    title: str
    body: str                     # markdown
    source: str = ""              # safe context description (redacted)
    tags: tuple = ()
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "title": self.title,
            "body": self.body,
            "source": self.source,
            "tags": list(self.tags),
            "confidence": round(float(self.confidence), 2),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeItem":
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**payload)


@dataclass
class KnowledgeEntry:
    """One retrieved document from the shared repository."""

    path: str
    category: str
    title: str
    body: str


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "knowledge").lower()).strip("-")
    return slug[:60] or "knowledge"


def _markdown_for(item: KnowledgeItem) -> str:
    front = (
        "---\n"
        f"title: {item.title}\n"
        f"category: {item.category}\n"
        f"confidence: {round(float(item.confidence), 2)}\n"
        + (f"tags: {', '.join(item.tags)}\n" if item.tags else "")
        + (f"source: {item.source}\n" if item.source else "")
        + "---\n\n"
    )
    return front + (item.body or "").strip() + "\n"


# ----------------------------------------------------------------------
# HTTP client (stdlib only, injectable for tests)
# ----------------------------------------------------------------------


class GitHubApiClient:
    """Minimal GitHub REST v3 client used by save/retrieve operations.

    Authentication failures and network errors raise :class:`KnowledgeError`
    with clean messages that never include the token.
    """

    def __init__(self, config: KnowledgeConfig):
        self.config = config
        self._tree_cache: list[str] | None = None

    # -- primitives -----------------------------------------------------

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.config.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        headers.update(extra or {})
        return headers

    def _request(self, method: str, url: str, payload=None, accept=None):
        headers = self._headers({"Accept": accept} if accept else None)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, body
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise KnowledgeError(
                "GitHub is unreachable (network failure). Knowledge was NOT saved."
            ) from exc

    def _map_http_error(self, exc: urllib.error.HTTPError) -> KnowledgeError:
        status = exc.code
        friendly = {
            401: "GitHub authentication failed (token rejected). Knowledge was NOT saved.",
            403: "GitHub permission denied for this repository/token. Knowledge was NOT saved.",
            404: "GitHub repository or path not found (check AUTOFIX_KNOWLEDGE_REPO). Knowledge was NOT saved.",
            409: "GitHub merge conflict while saving knowledge.",
            422: "GitHub rejected the content (validation or conflict). Knowledge was NOT saved.",
        }.get(status)
        if friendly:
            return KnowledgeError(friendly)
        return KnowledgeError(f"GitHub request failed (HTTP {status}).")

    # -- higher level ---------------------------------------------------

    def get_json(self, path: str):
        url = self.config.api_url.rstrip("/") + path
        _status, body = self._request("GET", url)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise KnowledgeError("GitHub returned an invalid response.") from exc

    def put_contents(self, path: str, content_text: str, message: str, sha: str | None):
        url = (
            self.config.api_url.rstrip("/")
            + "/repos/"
            + quote(self.config.owner, safe="")
            + "/"
            + quote(self.config.name, safe="")
            + "/contents/"
            + quote(path, safe="/")
        )
        payload = {
            "message": message,
            "content": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
            "branch": self.config.ref,
        }
        if sha:
            payload["sha"] = sha
        _status, body = self._request("PUT", url, payload=payload)
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        commit = ((data.get("commit") or {}).get("sha") or "")[:12]
        html_url = (data.get("content") or {}).get("html_url") or ""
        return {"commit": commit, "html_url": html_url}

    def file_exists_sha(self, path: str) -> str | None:
        url = (
            self.config.api_url.rstrip("/")
            + "/repos/"
            + quote(self.config.owner, safe="")
            + "/"
            + quote(self.config.name, safe="")
            + "/contents/"
            + quote(path, safe="/")
            + f"?ref={quote(self.config.ref, safe='')}"
        )
        try:
            _status, body = self._request("GET", url)
        except KnowledgeError as exc:
            if "not found" in str(exc).lower():
                return None
            raise
        try:
            return json.loads(body).get("sha")
        except ValueError:
            return None

    def list_markdown_paths(self) -> list[str]:
        """All ``*.md`` blob paths under the knowledge dir (cached per client)."""
        if self._tree_cache is not None:
            return self._tree_cache
        try:
            data = self.get_json("/repos/"
                                 + quote(self.config.owner, safe="")
                                 + "/"
                                 + quote(self.config.name, safe="")
                                 + f"/git/trees/{quote(self.config.ref, safe='')}?recursive=1")
        except KnowledgeError as exc:
            if "not found" in str(exc).lower():
                raise KnowledgeError(
                    "GitHub knowledge repository unavailable (invalid repository "
                    "or branch)."
                ) from exc
            raise
        prefix = self.config.dirname.strip("/") + "/"
        paths = [
            item["path"]
            for item in data.get("tree", [])
            if item.get("type") == "blob"
            and str(item.get("path", "")).startswith(prefix)
            and str(item.get("path", "")).endswith(".md")
        ]
        self._tree_cache = paths
        return paths

    def fetch_content(self, path: str) -> str:
        url = (
            self.config.api_url.rstrip("/")
            + "/repos/"
            + quote(self.config.owner, safe="")
            + "/"
            + quote(self.config.name, safe="")
            + "/contents/"
            + quote(path, safe="/")
            + f"?ref={quote(self.config.ref, safe='')}"
        )
        _status, body = self._request(
            "GET", url, accept="application/vnd.github.raw"
        )
        return body


_default_client_factory = lambda config: GitHubApiClient(config)


def set_client_factory(factory) -> None:
    """Dependency-injection hook for tests (factory: KnowledgeConfig → client)."""
    global _default_client_factory
    _default_client_factory = factory


# ----------------------------------------------------------------------
# Public operations
# ----------------------------------------------------------------------


def save_knowledge(item: KnowledgeItem, config=None, client=None, workspace: str | None = None) -> dict:
    """Save ONE approved knowledge item to the shared repository.

    Security gate runs first: hard secrets block the save entirely, soft
    secret values are redacted before upload.  When GitHub is unreachable the
    item is persisted locally via :func:`save_pending_knowledge` for later sync.

    Returns a result dict:

        {"ok": bool, "path": str|None, "url": str, "message": str,
         "sanitized": bool, "pending_sync": bool}

    Never raises for operational failures -- callers receive ok=False with a
    clean message safe to display in the UI.
    """
    try:
        if item.category not in KNOWLEDGE_CATEGORIES:
            return {
                "ok": False, "path": None, "url": "",
                "message": f"Unknown knowledge category: {item.category}",
                "sanitized": False,
                "pending_sync": False,
            }
        vetting = vet_knowledge(item.title, item.body)
        if not vetting.ok:
            reasons = "; ".join(vetting.blocked_reasons)
            return {
                "ok": False, "path": None, "url": "",
                "message": f"Knowledge rejected by security scan ({reasons}). "
                           "Nothing was saved.",
                "sanitized": True,
                "pending_sync": False,
            }
        clean = KnowledgeItem(
            category=item.category,
            title=vetting.sanitized_title.strip() or "Untitled knowledge",
            body=vetting.sanitized_body,
            source=sanitize_optional(item.source),
            tags=tuple(item.tags),
            confidence=item.confidence,
        )

        api = client
        cfg_for_path = config
        if api is None:
            cfg_for_path = KnowledgeConfig.from_env()
            if cfg_for_path is None or not cfg_for_path.is_configured():
                return {
                    "ok": False, "path": None, "url": "",
                    "message": "Shared knowledge repository is not configured "
                               "(set AUTOFIX_KNOWLEDGE_REPO and GITHUB_TOKEN). "
                               "Nothing was saved.",
                    "sanitized": vetting.was_sanitized,
                    "pending_sync": False,
                }
            api = _default_client_factory(cfg_for_path)

        dirname = (
            cfg_for_path.dirname.strip("/") if cfg_for_path is not None
            else DEFAULT_DIRNAME
        )
        path = f"{dirname}/{clean.category}/{_slugify(clean.title)}.md"
        try:
            sha = api.file_exists_sha(path)
        except KnowledgeError:
            # GitHub unreachable -- persist locally for later sync.
            pending_result = save_pending_knowledge(clean, workspace=workspace)
            return {
                "ok": pending_result["ok"],
                "path": pending_result.get("path"),
                "url": "",
                "message": pending_result["message"],
                "sanitized": vetting.was_sanitized,
                "pending_sync": pending_result["ok"],
            }
        verb = "Update" if sha else "Add"
        commit_message = f"{verb} {clean.category} knowledge: {clean.title}"
        result = api.put_contents(path, _markdown_for(clean), commit_message, sha)
        action = "updated" if sha else "saved"
        return {
            "ok": True,
            "path": path,
            "url": result.get("html_url", ""),
            "message": f"AI knowledge saved to GitHub ({clean.category}: "
                       f"{clean.title} {action}).",
            "sanitized": vetting.was_sanitized,
            "pending_sync": False,
        }
    except KnowledgeError as exc:
        pending_result = save_pending_knowledge(item, workspace=workspace)
        if pending_result.get("ok"):
            return {
                "ok": True,
                "path": pending_result.get("path"),
                "url": "",
                "message": f"GitHub unavailable -- {pending_result['message']}",
                "sanitized": False,
                "pending_sync": True,
            }
        return {"ok": False, "path": None, "url": "", "message": str(exc),
                "sanitized": False, "pending_sync": False}
    except Exception as exc:  # absolute last resort -- never crash AutoFix/UI
        return {
            "ok": False, "path": None, "url": "",
            "message": f"Saving knowledge failed unexpectedly: {type(exc).__name__}.",
            "sanitized": False,
            "pending_sync": False,
        }


def sanitize_optional(text: str) -> str:
    from app.agents.knowledge_security import sanitize_text

    return sanitize_text(text or "")


_KEYWORD_RE = re.compile(r"[a-z0-9_]{4,}")

_STOP = {"that", "this", "with", "from", "have", "what", "when", "your",
         "about", "would", "should", "could", "there", "their", "which"}


def _keywords(query: str) -> list[str]:
    return sorted(set(_KEYWORD_RE.findall((query or "").lower())))


def retrieve_knowledge(
    query: str,
    config=None,
    client=None,
    limit: int = 3,
    categories=None,
    workspace: str | None = None,
) -> list[KnowledgeEntry]:
    """Relevance-filtered retrieval of SHARED guidance.

    Only documents whose filename/category actually overlap the query
    keywords are fetched -- the repository is never loaded wholesale.
    When GitHub is unreachable, cached entries are served as fallback.
    Operational failures return [] (shared guidance is optional context;
    Chat/AutoFix must keep working without it).
    """
    try:
        cfg = config
        api = client
        if api is None:
            cfg = cfg or KnowledgeConfig.from_env()
            if cfg is None or not cfg.is_configured():
                return retrieve_cached_knowledge(query, workspace=workspace)
            api = _default_client_factory(cfg)
        dirname_lower = (cfg.dirname if cfg else DEFAULT_DIRNAME).lower()
        keywords = [
            k for k in _keywords(query)
            if k not in _STOP and k != dirname_lower
        ]
        if not keywords:
            return []
        wanted_categories = {c for c in (categories or KNOWLEDGE_CATEGORIES)}

        scored: list[tuple[int, int, str]] = []
        for index, path in enumerate(api.list_markdown_paths()):
            parts = Path(path).parts
            if len(parts) < 2:
                continue
            category = parts[-2]
            if category not in wanted_categories:
                continue
            haystack = path.lower()
            overlap = sum(1 for keyword in keywords if keyword in haystack)
            if overlap == 0:
                continue
            scored.append((-overlap, index, path))

        scored.sort()
        entries: list[KnowledgeEntry] = []
        for _neg_overlap, _index, path in scored[:limit]:
            body = api.fetch_content(path)
            entries.append(
                KnowledgeEntry(
                    path=path,
                    category=Path(path).parts[-2],
                    title=_title_from_markdown(body, Path(path).stem),
                    body=body,
                )
            )
        # Cache successful retrievals for offline use.
        if entries:
            try:
                save_to_cache(entries, query, workspace=workspace)
            except Exception:
                pass
        return entries
    except KnowledgeError:
        return retrieve_cached_knowledge(query, workspace=workspace)
    except Exception:
        return retrieve_cached_knowledge(query, workspace=workspace)


def _title_from_markdown(body: str, fallback: str) -> str:
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("title:"):
            return stripped.split(":", 1)[1].strip() or fallback
        if stripped.startswith("#"):
            return stripped.lstrip("# ").strip() or fallback
    return fallback


def shared_knowledge_block(query: str, limit: int = 2, max_chars: int = 1200) -> str:
    """Formatted shared-guidance block for prompts ("" when nothing found).

    Callers MUST embed :data:`PRIORITY_RULE` semantics — the block already
    carries them so downstream planners cannot miss the hierarchy.
    """
    entries = retrieve_knowledge(query, limit=limit)
    if not entries:
        return ""
    lines = [PRIORITY_RULE, ""]
    budget = max_chars - sum(len(line) + 1 for line in lines)
    for entry in entries:
        snippet = " ".join(entry.body.split())[:400]
        line = f"- [{entry.category}] {entry.title}: {snippet}"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line) + 1
    if len(lines) <= 2:
        return ""
    header = "Relevant shared AI knowledge:\n"
    return header + "\n".join(lines) + "\n"


@dataclass
class KnowledgeRetrievalStats:
    queries: int = 0
    hits: int = 0
    last_error: str = field(default="")


def knowledge_status(config=None) -> dict:
    """Safe configuration status for the UI (never includes the token)."""
    cfg = config or KnowledgeConfig.from_env()
    if cfg is None:
        return {
            "configured": False,
            "repo": "",
            "hint": "Set AUTOFIX_KNOWLEDGE_REPO and GITHUB_TOKEN to enable "
                    "the shared AI knowledge repository.",
        }
    return {
        "configured": True,
        "repo": cfg.repo,
        "ref": cfg.ref,
        "dirname": cfg.dirname,
        "hint": "",
    }


# ----------------------------------------------------------------------
# Local persistence — pending sync & offline cache
# ----------------------------------------------------------------------


def _local_base(workspace: str | None = None) -> Path:
    """Return the local knowledge base directory under the workspace root."""
    if workspace:
        return Path(workspace) / _LOCAL_KNOWLEDGE_DIR
    return Path(_LOCAL_KNOWLEDGE_DIR)


def _pending_dir(workspace: str | None = None) -> Path:
    d = _local_base(workspace) / _PENDING_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_dir(workspace: str | None = None) -> Path:
    d = _local_base(workspace) / _CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_pending_knowledge(
    item: KnowledgeItem, workspace: str | None = None
) -> dict:
    """Persist an approved knowledge item locally when GitHub is unavailable.

    The item is saved under ``<workspace>\\.autofix\\knowledge\\pending\\``
    as a JSON file.  When GitHub connectivity is restored the caller should
    process the pending queue and upload via :func:`save_knowledge`.

    Returns a result dict:
        {"ok": bool, "path": str|None, "message": str}
    """
    try:
        if item.category not in KNOWLEDGE_CATEGORIES:
            return {
                "ok": False, "path": None,
                "message": f"Unknown knowledge category: {item.category}",
            }
        vetting = vet_knowledge(item.title, item.body)
        if not vetting.ok:
            reasons = "; ".join(vetting.blocked_reasons)
            return {
                "ok": False, "path": None,
                "message": f"Knowledge rejected by security scan ({reasons}). Nothing was saved locally.",
            }
        clean = KnowledgeItem(
            category=item.category,
            title=vetting.sanitized_title.strip() or "Untitled knowledge",
            body=vetting.sanitized_body,
            source=sanitize_optional(item.source),
            tags=tuple(item.tags),
            confidence=item.confidence,
        )
        slug = _slugify(clean.title)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{ts}_{slug}.json"
        dest = _pending_dir(workspace) / filename
        payload = {
            "item": clean.to_dict(),
            "created_at": ts,
            "source": "pending_github_sync",
        }
        dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "ok": True,
            "path": str(dest),
            "message": f"Knowledge saved locally (pending sync): {clean.category}/{clean.title}.",
        }
    except Exception as exc:
        return {
            "ok": False, "path": None,
            "message": f"Local save failed unexpectedly: {type(exc).__name__}.",
        }


def list_pending_knowledge(workspace: str | None = None) -> list[dict]:
    """Return all locally-persisted pending knowledge items."""
    pending = _pending_dir(workspace)
    items: list[dict] = []
    for p in sorted(pending.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            raw["_file"] = str(p)
            items.append(raw)
        except Exception:
            continue
    return items


def pop_pending_knowledge(workspace: str | None = None) -> list[dict]:
    """Return and DELETE all pending items (after successful GitHub upload)."""
    items = list_pending_knowledge(workspace)
    pending = _pending_dir(workspace)
    for item in items:
        fpath = Path(item.pop("_file", ""))
        if fpath.exists():
            try:
                fpath.unlink()
            except OSError:
                pass
    return items


def save_to_cache(
    entries: list[KnowledgeEntry],
    query: str,
    workspace: str | None = None,
) -> None:
    """Cache retrieved knowledge entries for offline use."""
    cache = _cache_dir(workspace)
    key = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:60] or "default"
    cache_file = cache / f"{key}.json"
    payload = {
        "query": query,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "entries": [
            {"path": e.path, "category": e.category, "title": e.title, "body": e.body}
            for e in entries
        ],
    }
    try:
        cache_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def retrieve_cached_knowledge(
    query: str,
    workspace: str | None = None,
    max_age_seconds: int = 86400,
) -> list[KnowledgeEntry]:
    """Retrieve cached knowledge entries that are still fresh enough.

    Default max age is 24 hours. Returns [] when cache is missing or stale.
    """
    cache = _cache_dir(workspace)
    key = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:60] or "default"
    cache_file = cache / f"{key}.json"
    if not cache_file.exists():
        return []
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_at = raw.get("cached_at", "")
        if cached_at:
            from datetime import datetime as _dt
            try:
                age = (_dt.now(_dt.timezone.utc) - _dt.fromisoformat(cached_at)).total_seconds()
                if age > max_age_seconds:
                    return []
            except (ValueError, TypeError):
                return []
        return [
            KnowledgeEntry(path=e["path"], category=e["category"],
                           title=e["title"], body=e["body"])
            for e in raw.get("entries", [])
        ]
    except Exception:
        return []


def cleanup_local_knowledge(workspace: str | None = None) -> dict:
    """Remove all local knowledge files (pending + cache). Returns counts."""
    counts = {"pending": 0, "cache": 0}
    for f in _pending_dir(workspace).glob("*.json"):
        try:
            f.unlink()
            counts["pending"] += 1
        except OSError:
            pass
    for f in _cache_dir(workspace).glob("*.json"):
        try:
            f.unlink()
            counts["cache"] += 1
        except OSError:
            pass
    return counts
