"""Automatic web research for Chat AI.

Provides automatic web/forum research that triggers BEFORE the Chat AI
answers questions that benefit from current external information.  The user
is never asked for permission — research is an internal capability.

Architecture:
    Message → should_research() → build_search_queries() → web_search()
    → classify_source() → aggregate_results() → format_research_context()

The research context is passed to the configured Chat AI provider as extra
system context; the provider synthesizes a concise answer with citations.

Security:
    - All search queries are sanitized via redact_secrets() before sending
    - Never sends API keys, tokens, passwords, or credentials externally
    - Workspace/project context is never sent to external search engines

Provider independence:
    - Research is provider-agnostic; any configured provider can use the context
    - If the provider supports native web search, that capability is preferred
    - Without any provider, research results are available but not synthesized
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# ----------------------------------------------------------------------
# Research trigger detection
# ----------------------------------------------------------------------

#: Patterns indicating the message would benefit from current web information.
_RESEARCH_TRIGGER_PATTERNS = (
    # Latest/current version questions
    re.compile(
        r"\b(latest|current|newest|recent|new|updated|release|version)\s+"
        r"(of|for|in)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat('s| is| are) the (latest|current|newest|recent) version\b",
        re.IGNORECASE,
    ),
    # API behavior / documentation questions
    re.compile(
        r"\b(how (do|does|can|should|would)|api|documentation|docs|"
        r"endpoint|request|response)\b.*\b(work|use|call|implement|integrate)\b",
        re.IGNORECASE,
    ),
    # Error / troubleshooting
    re.compile(
        r"\b(error|exception|traceback|fail|broken|crash|bug|issue|"
        r"problem|trouble)\b.{0,60}\b(how|fix|solve|resolve|cause|why)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(how|fix|solve|resolve|cause|why)\b.{0,60}\b"
        r"(error|exception|traceback|fail|broken|crash|bug|issue|problem)\b",
        re.IGNORECASE,
    ),
    # Best practices / recommendations
    re.compile(
        r"\b(best|recommended|preferred|good|proper|correct)\s+"
        r"(practice|practices|way|approach|method|technique|pattern)\b",
        re.IGNORECASE,
    ),
    # Comparison / alternatives
    re.compile(
        r"\b(compare|comparison|versus|vs\.?|differ|difference|"
        r"alternative|alternatives|better than|worse than)\b",
        re.IGNORECASE,
    ),
    # Library/framework questions where current info matters
    re.compile(
        r"\b(install|setup|set up|configure|getting started|quickstart|"
        r"tutorial|example|sample)\b.{0,40}\b"
        r"(for|with|using|in|on)\b",
        re.IGNORECASE,
    ),
    # Framework/library mention + question
    re.compile(
        r"\b(opencode|github|vscode|vs code|react|next\.?js|vue|angular|"
        r"svelte|django|flask|fastapi|express|node|npm|pyside|pyqt|"
        r"docker|kubernetes|k8s|aws|azure|gcp|vercel|netlify)\b",
        re.IGNORECASE,
    ),
    # Explicit search requests
    re.compile(
        r"\b(search|google|look (up|into|for)|find|lookup|check)\b.{0,30}\b"
        r"(web|online|internet|website|documentation)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(web|online|internet|website|documentation)\b.{0,30}\b"
        r"(search|google|look (up|into|for)|find|lookup|check)\b",
        re.IGNORECASE,
    ),
    # Questions about current behavior
    re.compile(
        r"\b(does|do|is|are|can|could|should|will|would)\b.{0,30}\b"
        r"(still|currently|now|today|recently)\b",
        re.IGNORECASE,
    ),
    # Community/forum mentions
    re.compile(
        r"\b(stack\s*overflow|reddit|community|forum|discussion|"
        r"github (issue|issues|discussion|discussions)|developer)\b",
        re.IGNORECASE,
    ),
    # Troubleshooting with error messages
    re.compile(
        r"\b(traceback|stacktrace|stack trace|error message|"
        r"exception in|TypeError|ValueError|KeyError|IndexError|"
        r"ImportError|ModuleNotFoundError|FileNotFoundError|"
        r"ConnectionError|TimeoutError|PermissionError|"
        r"SyntaxError|RuntimeError|AttributeError)\b",
        re.IGNORECASE,
    ),
)

#: Patterns that indicate the message does NOT need web research.
_NO_RESEARCH_PATTERNS = (
    # Pure greetings
    re.compile(
        r"^\s*(hi|hey|hello|yo|good\s+(morning|afternoon|evening)|"
        r"thanks|thank you|thx|ty|bye|goodbye|howdy|ok|okay|"
        r"cool|nice|great|awesome|perfect|lol|haha|hehe)\s*[!.,?]*\s*$",
        re.IGNORECASE,
    ),
    # Very short casual messages
    re.compile(r"^\s*\S{1,15}\s*[!?.]*\s*$", re.IGNORECASE),
    # Questions fully answerable from context
    re.compile(
        r"^(what|who|where|when)\s+(am i|are you|is this|is that|is it)\b",
        re.IGNORECASE,
    ),
)

#: Patterns that indicate a coding/project request (research may be useful
#: but the primary path is proposal generation).
_CODING_RESEARCH_PATTERNS = (
    re.compile(
        r"\b(fix|debug|repair|resolve|correct|patch|troubleshoot|diagnose)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(how|what|why|where)\b.{0,40}\b"
        r"(error|fail|crash|broken|bug|issue|problem)\b",
        re.IGNORECASE,
    ),
)


def should_research(message: str) -> bool:
    """Determine whether the message would benefit from web research.

    Returns True when external/current information could improve the answer.
    Returns False for greetings, casual conversation, and messages fully
    answerable from context.
    """
    text = (message or "").strip()
    if not text:
        return False

    # Never research for greetings or very short casual messages.
    for pattern in _NO_RESEARCH_PATTERNS:
        if pattern.match(text):
            return False

    # Check for research triggers.
    for pattern in _RESEARCH_TRIGGER_PATTERNS:
        if pattern.search(text):
            return True

    return False


# ----------------------------------------------------------------------
# Search query construction
# ----------------------------------------------------------------------

#: Stop words to strip from search queries.
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for", "of",
    "and", "or", "but", "not", "with", "this", "that", "can", "do",
    "does", "did", "was", "were", "are", "am", "be", "been", "being",
    "have", "has", "had", "would", "could", "should", "will", "shall",
    "may", "might", "must", "need", "i", "we", "you", "they", "he",
    "she", "me", "us", "them", "my", "our", "your", "their", "its",
    "how", "what", "why", "when", "where", "which", "who", "whom",
    "if", "then", "else", "so", "just", "also", "too", "very", "really",
    "some", "any", "all", "no", "yes", "now", "here", "there", "about",
})

#: Software/framework names to keep in queries regardless of stop words.
_KEEP_NAMES = frozenset({
    "opencode", "github", "vscode", "vs", "code", "react", "nextjs",
    "vue", "angular", "svelte", "django", "flask", "fastapi", "express",
    "node", "nodejs", "npm", "pyside", "pyqt", "docker", "kubernetes",
    "aws", "azure", "gcp", "vercel", "netlify", "python", "javascript",
    "typescript", "rust", "golang", "java", "kotlin", "swift", "csharp",
    "postgresql", "mysql", "sqlite", "mongodb", "redis", "elasticsearch",
})


def build_search_queries(message: str, max_queries: int = 2) -> list[str]:
    """Build focused search queries from the user's message.

    Returns up to ``max_queries`` focused queries suitable for web search.
    Conversational noise is stripped; technical terms are preserved.
    """
    text = (message or "").strip()
    if not text:
        return []

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)

    # Remove common question prefixes
    cleaned = re.sub(
        r"^(can you|could you|would you|please|pls|plz|i want to|i need to|"
        r"help me|let's|how (about|would|should|can|do)|"
        r"what (is|are|does|do)|why (is|does|do)|"
        r"where (is|are|does|do)|when (is|are|does|do)|"
        r"who (is|are|does|do)|is (it|there|this|that)|"
        r"are (there|they|we)|does (it|this|that|the))\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    # Remove trailing question marks and punctuation
    cleaned = re.sub(r"[?!.,;:]+$", "", cleaned).strip()

    if not cleaned:
        cleaned = text

    # Build query tokens, preserving keep-words and removing stop words
    words = re.findall(r"[a-zA-Z0-9+#._-]+", cleaned)
    query_words = [
        w for w in words
        if w.lower() in _KEEP_NAMES or w.lower() not in _STOP_WORDS
    ]

    if not query_words:
        # Fallback: use the original cleaned text
        query_words = words[:8]

    queries: list[str] = []

    # Primary query: full cleaned terms
    primary = " ".join(query_words[:10])
    if primary.strip():
        queries.append(primary)

    # Secondary query: if there's a specific tool/framework mention, create
    # a more focused query for community/forum results.
    tool_names = [w for w in words if w.lower() in _KEEP_NAMES]
    if tool_names and len(queries) < max_queries:
        # Add "issue" or "error" context if debugging
        debug_words = []
        if re.search(r"\b(error|fail|bug|issue|fix|crash|broken)\b", text, re.IGNORECASE):
            debug_words = ["issue"]
        secondary_parts = tool_names[:3] + debug_words
        secondary = " ".join(secondary_parts)
        if secondary.strip() and secondary != primary:
            queries.append(secondary)

    return queries[:max_queries]


# ----------------------------------------------------------------------
# Source classification (priority system)
# ----------------------------------------------------------------------

@dataclass
class ResearchResult:
    """One research result with source metadata."""
    title: str
    url: str
    snippet: str
    source_priority: int  # 1=official, 2=github, 3=forum, 4=other
    source_type: str      # "official", "github", "forum", "community", "other"
    query: str            # which query produced this result

    @property
    def domain(self) -> str:
        """Extract domain from URL."""
        try:
            parsed = urllib.parse.urlparse(self.url)
            host = parsed.hostname or ""
            parts = host.split(".")
            if len(parts) >= 2:
                return ".".join(parts[-2:])
            return host
        except Exception:
            return ""


def classify_source(url: str, title: str = "") -> tuple[int, str]:
    """Classify a URL into source priority and type.

    Returns (priority, type_name) where lower priority number = higher trust.

    Priority 1: Official documentation and primary sources
    Priority 2: GitHub repositories, issues, discussions, release notes
    Priority 3: Technical/community forums (Stack Overflow, Reddit, etc.)
    Priority 4: Other reputable web sources
    """
    url_lower = (url or "").lower()
    title_lower = (title or "").lower()
    combined = url_lower + " " + title_lower

    # Priority 1: Official documentation
    official_patterns = (
        r"docs\.",
        r"documentation\.",
        r"\.readthedocs\.",
        r"\.dev/docs",
        r"\.dev/learn",
        r"\.dev/reference",
        r"\.dev/tutorial",
        r"\.dev/guide",
        r"/docs/",
        r"/documentation/",
        r"/api/",
        r"/reference/",
        r"/guide/",
        r"/tutorial/",
        r"/handbook/",
        r"/manual/",
        r"learn\.",
        r"wiki\.",
        r"developer\.",
    )
    for pattern in official_patterns:
        if re.search(pattern, combined):
            return 1, "official"

    # Priority 2: GitHub sources
    github_patterns = (
        r"github\.com",
        r"github\.io",
        r"raw\.githubusercontent",
        r"gist\.github",
        r"github\.com.*release",
    )
    for pattern in github_patterns:
        if re.search(pattern, url_lower):
            if re.search(r"issue|discussion|pull|release|changelog", combined):
                return 2, "github"
            return 2, "github"

    # Priority 3: Forums and community
    forum_patterns = {
        "stackoverflow": "forum",
        "stack overflow": "forum",
        r"reddit\.com": "community",
        r"reddit\.co\.uk": "community",
        "discourse": "community",
        "forum": "forum",
        "community": "community",
        "discuss": "community",
        "answers": "community",
        "quora": "community",
        r"dev\.to": "community",
        "hashnode": "community",
        r"medium\.com": "community",
        "nitter": "community",
        r"hn\.allegro": "community",
        r"news\.ycombinator": "community",
    }
    for pattern, source_type in forum_patterns.items():
        if re.search(pattern, combined):
            return 3, source_type

    # Priority 4: Everything else
    return 4, "other"


# ----------------------------------------------------------------------
# Web search execution (stdlib only)
# ----------------------------------------------------------------------

_SEARCH_TIMEOUT = 10  # seconds per search request
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def web_search(query: str, max_results: int = 8) -> list[ResearchResult]:
    """Execute a web search and return structured results.

    Uses DuckDuckGo's HTML interface (no API key required).  Returns an
    empty list on failure — never raises exceptions to callers.
    """
    if not query or not query.strip():
        return []

    try:
        from app.agents.task_memory import redact_secrets
        query = redact_secrets(query)
    except Exception:
        pass

    # Additional local secret scrubbing before sending externally
    query = _scrub_query_secrets(query)

    results: list[ResearchResult] = []

    # Try DuckDuckGo Lite (HTML-only, lightweight)
    try:
        results = _search_ddg_lite(query, max_results)
    except Exception:
        pass

    # Fallback: DuckDuckGo HTML
    if not results:
        try:
            results = _search_ddg_html(query, max_results)
        except Exception:
            pass

    return results


def _scrub_query_secrets(query: str) -> str:
    """Additional local secret scrubbing for search queries."""
    result = query
    # Remove anything that looks like a key/token/password
    result = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", result)
    result = re.sub(r"\bghp_[A-Za-z0-9]{16,}\b", "[REDACTED]", result)
    result = re.sub(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "[REDACTED]", result)
    result = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED]", result)
    result = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "[REDACTED]", result)
    result = re.sub(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{10,}", "Bearer [REDACTED]", result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"((?:api[_-]?key|secret|token|password|passwd|pwd)"
        r'\s*[:=]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s"\']+)',
        r"\1[REDACTED]",
        result,
        flags=re.IGNORECASE,
    )
    # Remove raw IP addresses that might be internal
    result = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[IP]", result)
    # Remove localhost references
    result = re.sub(r"\blocalhost(:\d+)?\b", "[HOST]", result, flags=re.IGNORECASE)
    # Remove .env file contents
    result = re.sub(r"\.env\b.*?(secret|key|token|password)", ".env [REDACTED]", result, flags=re.IGNORECASE)
    return result


def _search_ddg_lite(query: str, max_results: int) -> list[ResearchResult]:
    """Search via DuckDuckGo Lite HTML interface."""
    url = "https://lite.duckduckgo.com/lite/"
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return _parse_ddg_results(html, query, max_results)


def _search_ddg_html(query: str, max_results: int) -> list[ResearchResult]:
    """Search via DuckDuckGo HTML interface (fallback)."""
    params = urllib.parse.urlencode({"q": query, "t": "h_", "ia": "web"})
    url = f"https://html.duckduckgo.com/html/?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return _parse_ddg_results(html, query, max_results)


def _parse_ddg_results(html: str, query: str, max_results: int) -> list[ResearchResult]:
    """Parse DuckDuckGo HTML response into ResearchResults."""
    results: list[ResearchResult] = []

    # DuckDuckGo result link pattern: <a rel="nofollow" class="result-link" href="URL">TITLE</a>
    # followed by <td class="result-snippet">SNIPPET</td>
    link_pattern = re.compile(
        r'<a[^>]*?(?:rel="nofollow")?[^>]*?class="[^"]*result-link[^"]*"[^>]*?'
        r'href="([^"]*?)"[^>]*?>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    snippet_pattern = re.compile(
        r'<td[^>]*?class="[^"]*result-snippet[^"]*"[^>]*?>(.*?)</td>',
        re.DOTALL | re.IGNORECASE,
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (url, title_html) in enumerate(links[:max_results]):
        if not url or url.startswith("#") or "duckduckgo" in url.lower():
            continue
        # Clean HTML tags from title
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()

        priority, source_type = classify_source(url, title)
        results.append(ResearchResult(
            title=title,
            url=url,
            snippet=snippet,
            source_priority=priority,
            source_type=source_type,
            query=query,
        ))

    # Sort by priority (official first, then github, then forums, then other)
    results.sort(key=lambda r: r.source_priority)
    return results[:max_results]


# ----------------------------------------------------------------------
# Result aggregation and formatting
# ----------------------------------------------------------------------


def aggregate_results(all_results: list[ResearchResult], max_per_priority: int = 3) -> list[ResearchResult]:
    """Deduplicate and aggregate results across multiple queries.

    Keeps up to ``max_per_priority`` results per source priority level,
    preferring unique domains for diversity.  Results are sorted by
    source priority (official first).
    """
    seen_urls: set[str] = set()
    seen_domains: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
    aggregated: list[ResearchResult] = []

    # Sort by priority first
    sorted_results = sorted(all_results, key=lambda r: r.source_priority)

    for result in sorted_results:
        if result.url in seen_urls:
            continue
        domain = result.domain
        priority_bucket = seen_domains.get(result.source_priority, [])
        if len(priority_bucket) >= max_per_priority:
            continue
        seen_urls.add(result.url)
        priority_bucket.append(domain)
        seen_domains.setdefault(result.source_priority, []).append(domain)
        aggregated.append(result)

    return aggregated


def format_research_context(results: list[ResearchResult]) -> str:
    """Format research results as context for the Chat AI provider.

    Returns a concise, structured context block that the provider can use
    to synthesize a citation-backed answer.  Does NOT dump raw search
    results — the provider is expected to analyze and synthesize.
    """
    if not results:
        return ""

    lines = ["Web research results (for reference — synthesize, do not copy):"]

    for i, result in enumerate(results, 1):
        priority_label = {
            1: "OFFICIAL",
            2: "GITHUB",
            3: "COMMUNITY",
            4: "WEB",
        }.get(result.source_priority, "WEB")

        snippet = result.snippet[:300] if result.snippet else ""
        lines.append(
            f"\n{i}. [{priority_label}] {result.title}\n"
            f"   URL: {result.url}\n"
            f"   Source: {result.source_type} ({result.domain})\n"
            f"   Snippet: {snippet}"
        )

    lines.append(
        "\nInstructions: Use these research results to provide an accurate, "
        "current answer. Cite sources where appropriate. Cross-check "
        "conflicting information. Prefer official documentation over forum "
        "posts. When community experience is useful, mention it with context."
    )

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Main research entry point
# ----------------------------------------------------------------------


@dataclass
class ResearchContext:
    """Aggregated research output for one message."""
    should_research: bool
    queries: list[str] = field(default_factory=list)
    results: list[ResearchResult] = field(default_factory=list)
    context_text: str = ""
    failed: bool = False
    error: str = ""


def research_message(message: str) -> ResearchContext:
    """Full research pipeline for one chat message.

    Returns a ResearchContext with results ready to be injected into the
    Chat AI provider's context.  Never raises exceptions — failures are
    captured in the result.
    """
    ctx = ResearchContext(should_research=should_research(message))

    if not ctx.should_research:
        return ctx

    # Build search queries
    ctx.queries = build_search_queries(message)
    if not ctx.queries:
        ctx.should_research = False
        return ctx

    # Execute searches
    all_results: list[ResearchResult] = []
    for query in ctx.queries:
        try:
            results = web_search(query, max_results=6)
            all_results.extend(results)
        except Exception as exc:
            ctx.failed = True
            ctx.error = str(exc)

    # Aggregate and deduplicate
    ctx.results = aggregate_results(all_results)

    # Format context for the provider
    ctx.context_text = format_research_context(ctx.results)

    return ctx
