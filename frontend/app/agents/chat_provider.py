"""Conversational AI provider layer for Chat mode.

Detection mirrors ``app.agents.chat_agents`` (same environment signals) but
this module can actually hold a conversation:

    GPT      — OPENAI_API_KEY            (api.openai.com)
    Claude   — ANTHROPIC_API_KEY         (api.anthropic.com)
    DeepSeek — DEEPSEEK_API_KEY          (api.deepseek.org)
    AUTOFIX  — AUTOFIX_PROVIDER in       (AUTOFIX_BASE_URL, OpenAI-compatible
               {openai, openai-compatible,     chat/completions endpoint)
                anthropic, claude, deepseek}
               plus AUTOFIX_API_KEY / AUTOFIX_BASE_URL / AUTOFIX_MODEL

When no cloud provider is configured the :class:`LocalAssistant` answers.  It
is an honest built-in helper: it clearly identifies itself as offline, never
pretends to be a cloud model, and only inspects the workspace when the user
explicitly asks about project files.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from app.agents.intent import (
    CODING_CATEGORIES,
    QUESTION,
    classify_intent,
)

REQUEST_TIMEOUT_SECONDS = 60

_OPENAI_DEFAULTS = ("https://api.openai.com/v1", "gpt-4o-mini")
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_DEFAULT_MODEL = "claude-3-5-haiku-latest"
_DEEPSEEK_DEFAULTS = ("https://api.deepseek.org", "deepseek-chat")

_AUTOFIX_PROVIDER_KEYS = {"openai", "openai-compatible", "anthropic", "claude", "deepseek"}


class ChatProviderError(Exception):
    """Raised when every configured cloud provider failed."""

    def __init__(self, detail: str = ""):
        super().__init__(detail or "Unable to reach the configured AI provider.")
        self.detail = detail


@dataclass(frozen=True)
class ProviderConfig:
    """One callable conversational provider."""

    name: str
    kind: str  # "openai" | "anthropic"
    base_url: str
    api_key: str
    model: str
    #: Optional extra system context (e.g. redacted project memory) injected
    #: alongside the base system prompt.  Never contains credentials — memory
    #: content is secret-redacted before it is stored AND before it is sent.
    system_context: str = ""


def _env(env, key):
    return str(env.get(key, "")).strip()


def available_providers(env=None) -> list[ProviderConfig]:
    """Ordered list of configured providers (first entry is tried first)."""
    env = os.environ if env is None else env
    providers: list[ProviderConfig] = []

    if _env(env, "OPENAI_API_KEY"):
        providers.append(
            ProviderConfig(
                "GPT", "openai",
                _env(env, "OPENAI_BASE_URL") or _OPENAI_DEFAULTS[0],
                _env(env, "OPENAI_API_KEY"),
                _env(env, "OPENAI_MODEL") or _OPENAI_DEFAULTS[1],
            )
        )

    if _env(env, "ANTHROPIC_API_KEY"):
        providers.append(
            ProviderConfig(
                "Claude", "anthropic",
                _env(env, "ANTHROPIC_BASE_URL") or _ANTHROPIC_URL,
                _env(env, "ANTHROPIC_API_KEY"),
                _env(env, "ANTHROPIC_MODEL") or _ANTHROPIC_DEFAULT_MODEL,
            )
        )

    if _env(env, "DEEPSEEK_API_KEY"):
        providers.append(
            ProviderConfig(
                "DeepSeek", "openai",
                _env(env, "DEEPSEEK_BASE_URL") or _DEEPSEEK_DEFAULTS[0],
                _env(env, "DEEPSEEK_API_KEY"),
                _env(env, "DEEPSEEK_MODEL") or _DEEPSEEK_DEFAULTS[1],
            )
        )

    provider = _env(env, "AUTOFIX_PROVIDER").lower()
    autofix_key = _env(env, "AUTOFIX_API_KEY")
    autofix_url = _env(env, "AUTOFIX_BASE_URL")
    if provider in _AUTOFIX_PROVIDER_KEYS and autofix_key and autofix_url:
        model = _env(env, "AUTOFIX_MODEL") or _OPENAI_DEFAULTS[1]
        kind = "anthropic" if provider in ("anthropic", "claude") else "openai"
        base = autofix_url.rstrip("/")
        if kind == "openai" and not base.endswith("/v1") and "/v1" not in base:
            pass  # caller-supplied URL is used verbatim
        providers.append(
            ProviderConfig("AutoFix Model", kind, base, autofix_key, model)
        )

    return providers


def provider_chain(env=None) -> list[ProviderConfig]:
    """Ordered provider chain for automatic connection + fallback.

    The app-configured AutoFix model (AUTOFIX_PROVIDER/AUTOFIX_API_KEY/
    AUTOFIX_BASE_URL) is preferred when present; every other configured
    provider follows.  ``AUTOFIX_CHAT_PROVIDER`` (fuzzy name match, e.g.
    "deepseek" or "claude") overrides the preferred entry.  Selection is
    fully automatic — the user never picks a Chat provider.
    """
    env = os.environ if env is None else env
    chain = list(available_providers(env))
    if not chain:
        return []

    def _preferred_index() -> int:
        override = _env(env, "AUTOFIX_CHAT_PROVIDER").lower()
        if override:
            for index, config in enumerate(chain):
                if override in config.name.lower():
                    return index
        # AUTOFIX_PROVIDER is the application-level preferred provider.
        if _env(env, "AUTOFIX_PROVIDER").lower() in _AUTOFIX_PROVIDER_KEYS:
            for index, config in enumerate(chain):
                if config.name == "AutoFix Model":
                    return index
        return -1

    index = _preferred_index()
    if index > 0:
        preferred = chain.pop(index)
        chain.insert(0, preferred)
    return chain


def _clean_provider_error(detail: str) -> str:
    """Strip any credential-looking material from provider error text."""
    try:
        from app.agents.task_memory import redact_secrets

        return redact_secrets(str(detail or ""))
    except Exception:
        return str(detail or "")


# ----------------------------------------------------------------------
# Project memory context (read-only, secret-redacted)
# ----------------------------------------------------------------------


def memory_context_block(workspace: str | Path, query: str, limit: int = 3) -> str:
    """Compact redacted project-memory context for cloud provider calls.

    Uses the EXISTING project-local ``.autofix\\memory`` system (keyword
    retrieval over errors → fixes → decisions → tasks/conversations).  Memory
    is already redacted when written; it is re-redacted here as defense in
    depth so API keys or secrets can never leak through a prompt.
    """
    try:
        from app.agents.task_memory import redact_secrets, retrieve_relevant

        records = retrieve_relevant(workspace, query or "", limit=limit)
    except Exception:
        return ""

    notes: list[str] = []
    for record in records:
        content = redact_secrets(str(record.get("content", "")))[:400]
        if not content.strip():
            continue
        notes.append(
            f"[{record.get('kind')}] {record.get('title')}: {content}"
        )
    if not notes:
        return ""
    return "Relevant project memory:\n" + "\n".join(notes)


def analyze(task: str, workspace: str | Path, env=None) -> tuple[str | None, str]:
    """Planning/analysis through the shared provider layer.

    Returns ``(plan_text, source_note)``.  When no provider is configured the
    result is ``(None, "")`` and callers fall back to the deterministic local
    planner.  When providers ARE configured but every call fails, the last
    error is returned so the caller can surface an honest note.

    Planning context priority (locked architecture): current project files →
    project configuration (workspace context) → project memory → shared AI
    knowledge → task text.
    """
    chain = provider_chain(env)
    if not chain:
        return None, ""
    from app.agents.github_knowledge import shared_knowledge_block

    context = workspace_context_block(workspace)
    shared = ""
    try:
        shared = shared_knowledge_block(task, limit=2)
    except Exception:
        shared = ""
    prompt = (
        "You are the AutoFix planner inside AutoFix AI Studio.\n"
        "Produce a concrete step-by-step implementation plan for the task "
        "below. Analysis only — do NOT claim to have executed anything."
        + (f"\n\n{context}" if context else "")
        + (f"\n\n{shared}" if shared else "")
        + f"\n\nTask:\n{task}"
    )
    failed: list[str] = []
    last_error: ChatProviderError | None = None
    for config in chain:
        try:
            return call_provider(config, prompt), config.name
        except ChatProviderError as exc:
            failed.append(config.name)
            last_error = ChatProviderError(_clean_provider_error(exc.detail))
    return None, (
        str(last_error or "provider failed")
        + (f" [tried: {', '.join(failed)}]" if failed else "")
    )


# ----------------------------------------------------------------------
# HTTP calls (stdlib only — no new dependencies)
# ----------------------------------------------------------------------


def call_provider(config: ProviderConfig, message: str, history=None) -> str:
    history = list(history or [])
    if config.kind == "anthropic":
        return _call_anthropic(config, message, history)
    return _call_openai_compatible(config, message, history)


def _post_json(url, payload, headers, provider_name="provider"):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise ChatProviderError(f"{provider_name} HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ChatProviderError(f"{provider_name} connection failed: {exc}") from exc


def _system_prompt(config: ProviderConfig) -> str:
    system = (
        "You are AutoFix Assistant, the conversational AI inside the "
        "AutoFix AI Studio IDE — a desktop Python project (PySide6 frontend, "
        "FastAPI backend) with an AutoFix execution pipeline.\n\n"
        "Behavior:\n"
        "- Answer naturally and conversationally. Be concise for simple "
        "questions; be structured (headings, numbered plans) for complex "
        "tasks.\n"
        "- Understand follow-ups: pronouns like 'it' or 'that' refer to the "
        "most recent relevant subject in the conversation.\n"
        "- Reason about requirements, constraints, dependencies, edge cases "
        "and verification internally; present only concise conclusions and "
        "useful summaries — never expose private step-by-step reasoning.\n"
        "- For coding or project changes: discuss, analyze and propose. "
        "Never claim to have executed anything and never promise execution; "
        "execution happens exclusively through the separate AutoFix pipeline "
        "after the user's explicit approval in the UI.\n"
        "- Architecture facts you must respect when advising: Bulk is an "
        "input path into AutoFix (not an engine); WorkerRouter is the single "
        "routing authority; OpenCode/DeepSeek/Copilot are internal workers, "
        "not user-facing chat modes.\n"
        "- If a request is ambiguous in a way that materially changes the "
        "result, ask one specific clarifying question; otherwise propose a "
        "sensible default instead of interrogating the user.\n"
        "- When web research results are provided in the system context, "
        "use them to give current, accurate answers. Synthesize the "
        "research into a clear response with source citations. Prefer "
        "official documentation over forum posts. When community "
        "experience is useful, mention it with appropriate context."
    )
    if config.system_context:
        system += "\n\n" + config.system_context
    return system


def _call_openai_compatible(config, message, history):
    messages = [
        {
            "role": "system",
            "content": _system_prompt(config),
        }
    ]
    messages.extend({"role": role, "content": text} for role, text in history)
    messages.append({"role": "user", "content": message})

    data = _post_json(
        config.base_url.rstrip("/") + "/chat/completions",
        {"model": config.model, "messages": messages},
        {"Authorization": f"Bearer {config.api_key}"},
        provider_name=config.name,
    )
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ChatProviderError(f"{config.name} returned an unexpected response.") from exc


def _call_anthropic(config, message, history):
    system = _system_prompt(config)
    messages = [{"role": role, "content": text} for role, text in history]
    messages.append({"role": "user", "content": message})

    data = _post_json(
        config.base_url,
        {"model": config.model, "max_tokens": 2048, "system": system, "messages": messages},
        {"x-api-key": config.api_key, "anthropic-version": "2023-06-01"},
        provider_name=config.name,
    )
    try:
        return str(data["content"][0]["text"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ChatProviderError(f"{config.name} returned an unexpected response.") from exc


def converse(
    message: str,
    workspace: str | Path,
    history=None,
    env=None,
    system_context: str | None = None,
) -> str:
    """Get a conversational reply with automatic provider connection.

    Providers are auto-detected from the environment (no manual selection).
    They are tried in preferred order; the first usable one answers.  The
    built-in local assistant is used ONLY when no cloud provider is
    configured at all — when providers exist but every connection fails, a
    clean :class:`ChatProviderError` is raised (credential-free).

    Cloud calls receive relevant redacted project memory (existing
    ``.autofix\\memory`` system) as extra system context, plus any explicit
    ``system_context`` block (e.g. the ChatEngine's selected context slice).
    """
    chain = provider_chain(env)
    if not chain:
        return LocalAssistant.respond(message, workspace, context_text=system_context or "")

    memory = memory_context_block(workspace, message)
    extra = "\n".join(filter(None, [memory, system_context or ""])).strip()
    failed: list[str] = []
    last_error: ChatProviderError | None = None
    for config in chain:
        if extra:
            config = replace(config, system_context=extra)
        try:
            return call_provider(config, message, history)
        except ChatProviderError as exc:
            failed.append(config.name)
            last_error = ChatProviderError(_clean_provider_error(exc.detail))
    names = ", ".join(failed) if failed else "configured providers"
    raise ChatProviderError(
        f"{last_error.detail} [tried: {names}]"
        if last_error
        else f"Unable to reach any configured AI provider ({names})."
    )


# ----------------------------------------------------------------------
# Built-in offline assistant
# ----------------------------------------------------------------------


_GREETINGS = ("hello", "hi ", "hi!", "hi?", "hey", "yo ", "good morning",
              "good afternoon", "good evening", "how are you", "howdy")

_WORKSPACE_INTENT_WORDS = (
    "file", "files", "project", "structure", "codebase", "folder",
    "directory", "workspace", "repo", "repository", "tree", "layout",
)

#: Small deterministic knowledge base for common informational questions.
#: Each entry is ``(pattern, answer)``; the first match wins.  The content is
#: static, factual reference material — the local assistant never invents
#: reasoning or claims a cloud model produced it.
_LOCAL_KNOWLEDGE: tuple[tuple[str, str], ...] = (
    (
        r"\bautofix\b",
        "AutoFix AI Studio is this IDE. Its AI panel has two modes:\n\n"
        "- Chat — standalone conversation with the configured Chat AI "
        "provider; it discusses, analyzes, plans and proposes, but never "
        "creates tasks or executes anything.\n"
        "- AutoFix — analyze → plan → your approval → execution through "
        "decomposition, agent assignment, WorkerRouter and verification, "
        "with same-task recovery. Large pastes are detected automatically "
        "and routed into AutoFix.\n\n"
        "Project memory persists under .autofix\\memory and task state under "
        ".autofix\\tasks inside the selected workspace; reusable AI "
        "knowledge can be shared to a separately configured GitHub repository "
        "with your explicit approval."
    ),
    (
        r"\bopencode\b",
        "OpenCode is an external coding-agent CLI integrated into AutoFix AI "
        "Studio. Approved AutoFix tasks are executed by OpenCode inside the "
        "selected workspace; large requests travel via a task file under "
        ".autofix\\tasks instead of one huge command line.",
    ),
    (
        r"\berror\b.*\b(means?|meaning|explain)\b|\b(explain|what does)\b.*\berror\b",
        "I can't see the specific error from here. Paste the full traceback "
        "or message into this chat — if you phrase it as a task ('fix this "
        "error: …'), AutoFix will diagnose and repair it in the active "
        "workspace with your approval.",
    ),
    (
        r"\bpython\b",
        "Python is a high-level, general-purpose programming language known "
        "for readable syntax and a large standard library. It is interpreted, "
        "dynamically typed, and widely used for scripting, web backends "
        "(Django, Flask, FastAPI), automation, data analysis and AI.",
    ),
    (
        r"\breact\b",
        "React is a JavaScript library (from Meta) for building user "
        "interfaces out of components. You describe the UI as a function of "
        "state; React re-renders efficiently when state changes. Hooks such "
        "as useState and useEffect manage state and side effects, and JSX "
        "mixes markup with JavaScript.",
    ),
    (
        r"\b(css )?animations?\b|\btransition",
        "In CSS, 'transition' smoothly interpolates property changes (hover, "
        "focus, class changes), while '@keyframes' define multi-step "
        "animations attached via the 'animation' property (duration, easing, "
        "iteration). Prefer transform and opacity for smooth performance.",
    ),
    (
        r"\bcss\b",
        "CSS describes how HTML elements are displayed: rules select elements "
        "and apply properties (color, spacing, layout). The cascade and "
        "specificity decide which rule wins. Layout is typically done with "
        "flexbox or grid; motion uses transitions and keyframe animations.",
    ),
    (
        r"\bhtml\b",
        "HTML defines the structure of a web page as a tree of elements "
        "(<h1>, <p>, <a>, <form>…). Attributes configure elements, and "
        "semantic tags (<header>, <main>, <nav>) describe meaning for "
        "browsers and accessibility tools.",
    ),
    (
        r"\bjavascript\b|\btypescript\b",
        "JavaScript is the programming language of the web — dynamic, "
        "event-driven, running in every browser and on servers via Node.js. "
        "TypeScript is its statically typed superset that compiles to plain "
        "JavaScript, catching type errors at build time.",
    ),
    (
        r"\bgit\b",
        "Git is a distributed version-control system: you commit snapshots of "
        "your files, branch to develop features in isolation, and merge "
        "branches back together. Everyday commands: git status / add / "
        "commit, git checkout -b <branch>, git merge, git pull / push.",
    ),
    (
        r"\bapi\b|\brest\b",
        "An API is a contract that lets programs talk: defined endpoints, "
        "inputs and outputs. Web APIs commonly follow REST — resources "
        "addressed by URLs and manipulated over HTTP (GET reads, POST "
        "creates, PUT/PATCH updates, DELETE removes), exchanging JSON.",
    ),
    (
        r"\bhttp\b",
        "HTTP is the request/response protocol behind the web: a client sends "
        "a method (GET, POST, …), URL and headers, and the server replies "
        "with a status code (200 OK, 404 Not Found, 500 Server Error) plus a "
        "body. HTTPS encrypts the exchange with TLS.",
    ),
    (
        r"\bjson\b",
        "JSON is a minimal, language-independent data format: objects are "
        "{\"key\": value} maps, arrays are ordered lists, and values are "
        "strings, numbers, booleans, null, or nested objects/arrays. It is "
        "the standard payload format for web APIs and config files.",
    ),
    (
        r"\bsql\b|\bdatabase\b",
        "SQL is the standard language for relational databases: define tables "
        "with CREATE TABLE, query with SELECT … FROM … WHERE, join related "
        "tables, and modify rows with INSERT / UPDATE / DELETE. Common "
        "engines include SQLite, PostgreSQL and MySQL.",
    ),
    (
        r"\bdocker\b",
        "Docker packages an application and its dependencies into an image; "
        "running instances of images are containers that behave identically "
        "across machines. A Dockerfile describes the build steps; 'docker "
        "build' creates the image and 'docker run' starts a container.",
    ),
    (
        r"\bvenv\b|\bvirtual ?env(ironment)?s?\b|\bpip\b",
        "A virtual environment isolates a project's Python packages: "
        "'python -m venv .venv' creates one, '.venv\\Scripts\\activate' "
        "(Windows) activates it, and 'pip install -r requirements.txt' "
        "installs pinned dependencies only inside it.",
    ),
    (
        r"\bpytest\b",
        "pytest is Python's mainstream test framework: write test_* functions "
        "with plain assert statements, share setup through fixtures, "
        "parametrize repeated cases, and run everything with the 'pytest' "
        "command — failures are reported with full assertion details.",
    ),
    (
        r"\bnode\b|\bnpm\b|\bnpx\b",
        "Node.js runs JavaScript outside the browser, and npm is its package "
        "manager. package.json declares dependencies and scripts; 'npm "
        "install' downloads dependencies and 'npm run <script>' executes "
        "project tasks.",
    ),
    (
        r"\blogin (page|form|screen)\b|\blogin\b|\bsign ?up\b",
        "A login page collects credentials (usually email/username and "
        "password) in a form, validates them client-side, submits them to an "
        "authentication endpoint, and shows success (session/token stored) or "
        "error states. Visually it typically centers a card with fields, a "
        "submit button and helper links — easy to enhance with CSS "
        "animations/transitions.",
    ),
    (
        r"\bauth(entication|orization)?\b",
        "Authentication verifies who a user is: passwords are stored hashed "
        "(bcrypt/argon2), and login establishes either a server session "
        "(cookie) or a signed token (JWT) sent on each request. Authorization "
        "then decides what that user may do; OAuth allows sign-in via "
        "external providers instead of a password.",
    ),
    (
        r"\bdark mode\b",
        "Dark mode is an alternate low-glare color theme. It is usually "
        "implemented with CSS custom properties switched by a class or "
        "data-theme attribute, honored alongside the OS preference via the "
        "prefers-color-scheme media query, with the choice remembered in "
        "localStorage.",
    ),
    (
        r"\bmvc\b",
        "MVC separates an application into Model (data and business logic), "
        "View (presentation) and Controller (input handling that updates the "
        "model and selects the view) — keeping concerns independent and "
        "easier to test.",
    ),
    (
        r"\boop\b|\bobject oriented\b",
        "Object-oriented programming organizes code into classes that bundle "
        "state (fields) with behavior (methods). Its core principles are "
        "encapsulation, inheritance, polymorphism and abstraction.",
    ),
)

_LOCAL_KNOWLEDGE_RE = [
    (re.compile(pattern, re.IGNORECASE), answer)
    for pattern, answer in _LOCAL_KNOWLEDGE
]


def _lookup_local_knowledge(lowered_question: str) -> str | None:
    for pattern, answer in _LOCAL_KNOWLEDGE_RE:
        if pattern.search(lowered_question):
            return answer
    return None


def _is_greeting(lowered: str) -> bool:
    if lowered.strip() in ("hi", "hello", "hey", "yo"):
        return True
    return any(lowered.startswith(g) for g in _GREETINGS)


def _is_followup(lowered: str) -> bool:
    """Short messages that lean on prior context (pronouns / ellipsis)."""
    if len(lowered.split()) > 12:
        return False
    return bool(re.search(
        r"\b(it|that|this|those|these|them|also|again)\b"
        r"|^(how|what|why|and)\b",
        lowered,
    ))


class LocalAssistant:
    """Offline fallback assistant — honest about being local.

    It answers greetings, capability questions and a small built-in set of
    common programming questions deterministically.  It never pretends to be
    a cloud model and never blocks the user over a missing API key.  It is
    strictly a Chat-mode responder: it never routes anything to AutoFix,
    Bulk or OpenCode — build/fix work happens only in the mode the user
    explicitly selects.
    """

    @staticmethod
    def respond(message: str, workspace: str | Path, context_text: str = "") -> str:
        lowered = (message or "").strip().lower()

        if _is_greeting(lowered):
            return (
                "Hello! I'm AutoFix Assistant running in local mode — no cloud "
                "AI provider is configured, so I answer directly without a "
                "network call.\n\n"
                "I can list this workspace's contents and answer common "
                "programming questions. When you want files built or fixed, "
                "switch to AutoFix mode (large pastes are detected "
                "automatically) and the task will be planned for your "
                "approval. For full conversational AI you can optionally "
                "configure OPENAI_API_KEY, ANTHROPIC_API_KEY or "
                "DEEPSEEK_API_KEY."
            )

        if "how are you" in lowered:
            return (
                "Running fine, thanks! I'm the built-in local assistant right "
                "now — everything I do works offline."
            )

        if any(w in lowered for w in ("what can you do", "help", "capabilities", "who are you")):
            return (
                "I'm AutoFix Assistant. The AI panel has two modes:\n\n"
                "  Chat     — normal conversation (this mode): questions, "
                "explanations, analysis, planning and proposals. Never "
                "executes anything.\n"
                "  AutoFix  — workspace analysis, plan, APPROVE & EXECUTE pipeline "
                "for real build/fix work. Large pastes are detected automatically.\n\n"
                "Project-specific memory stays local under .autofix\\memory. "
                "Reusable AI knowledge can be saved to a separately configured "
                "GitHub repository — always with your explicit approval.\n\n"
                "In local mode I can also describe the workspace layout and "
                "answer common programming questions (Python, React, CSS, Git, "
                "APIs…). Configure a provider key for full conversational answers."
            )

        if any(w in lowered for w in ("thank", "thanks", "cheers")):
            return "You're welcome!"

        # Contextual follow-up: the engine passes the selected context slice
        # so "how would you do it?" can reference the current subject.
        if context_text and _is_followup(lowered):
            subject = ""
            for line in context_text.splitlines():
                if line.startswith("Current subject:"):
                    subject = line.split(":", 1)[1].strip()
                    break
            if subject:
                return (
                    f"Continuing with {subject} (still local mode, so this is "
                    "my deterministic take):\n\n"
                    "1. Re-check how that component works today.\n"
                    "2. Make the smallest change that achieves the goal.\n"
                    "3. Extend the tests around it.\n"
                    "4. Verify with the project suites.\n\n"
                    "Say the word and I'll prepare a concrete AutoFix "
                    "proposal — nothing executes without your approval."
                )

        if any(w in lowered for w in _WORKSPACE_INTENT_WORDS):
            listing = LocalAssistant.workspace_summary(workspace)
            if listing:
                return (
                    "Workspace overview (local mode — top-level entries of "
                    f"{Path(workspace).name}):\n\n{listing}"
                )
            return f"The workspace directory could not be read:\n{workspace}"

        intent = classify_intent(message)

        if intent.category == QUESTION:
            answer = _lookup_local_knowledge(lowered)
            if answer:
                return (
                    "Here's a short local answer (no cloud AI needed):\n\n"
                    + answer
                )
            return (
                "I'm running in local mode, so I can't reason deeply about "
                "that yet.\n\n"
                "Things I can still do right now:\n"
                "- Describe the current workspace ('show project files')\n"
                "- Answer common programming questions (Python, React, CSS, "
                "Git, APIs, Docker, pytest…)\n\n"
                "For build/fix work switch to AutoFix mode; large pastes are "
                "routed into the AutoFix pipeline automatically. "
                "Full conversational AI becomes available when a provider key "
                "is configured."
            )

        if intent.category in CODING_CATEGORIES:
            return (
                "That looks like a coding task for your project. Switch to "
                "AutoFix mode to plan and execute it with your approval — or "
                "just describe it here and I'll prepare a proposal that "
                "AutoFix executes after you approve. Chat mode only talks; "
                "it never modifies your project directly."
            )

        return (
            "I'm the built-in local assistant — I work fully offline, no "
            "cloud AI required.\n\n"
            "Ask me about the workspace or common programming topics. For "
            "build/fix work ('create a login page', 'fix this error', 'add "
            "dark mode') switch to AutoFix mode, where tasks are planned and "
            "executed with your approval."
        )

    @staticmethod
    def workspace_summary(workspace: str | Path, max_entries: int = 24) -> str:
        """Lightweight top-level listing (no recursive scan)."""
        root = Path(workspace)
        try:
            entries = sorted(
                root.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError:
            return ""

        ignored = {
            ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
            "node_modules", ".idea", ".vscode", "dist", "build",
        }
        lines: list[str] = []
        for path in entries:
            if path.name in ignored:
                continue
            marker = "[dir]  " if path.is_dir() else "[file] "
            lines.append(f"  {marker}{path.name}")
            if len(lines) >= max_entries:
                remaining = len(entries) - max_entries
                if remaining > 0:
                    lines.append(f"  … and {remaining} more entries")
                break
        return "\n".join(lines)


def workspace_context_block(workspace: str | Path) -> str:
    """Compact workspace context for cloud providers (top level only)."""
    summary = LocalAssistant.workspace_summary(workspace, max_entries=30)
    if not summary:
        return ""
    return f"Active workspace: {Path(workspace).resolve()}\nTop-level entries:\n{summary}\n"
