"""Conversational intelligence layer for Chat mode.

CHAT = THINK / DISCUSS / ANALYZE / PLAN / PROPOSE.
AUTOFIX = CREATE TASK / DECOMPOSE / DISTRIBUTE / EXECUTE / VERIFY / RECOVER.

This module upgrades Chat's conversational quality while preserving the
locked execution architecture:

* Intent refinement layers ON TOP OF the existing deterministic classifier
  (``app.agents.intent.classify_intent``) — there is no second competing
  intent system.  Coding categories keep their original meaning; this layer
  adds conversational granularity (greeting, explanation, analysis,
  brainstorm, recommendation, plan/proposal requests, clarification).
* A context builder selects RELEVANT information only: a bounded window of
  recent conversation turns, keyword-matched redacted project memory,
  related persisted AutoFix tasks and targeted workspace inspection.  The
  full project history is never sent anywhere blindly.
* Structured response models (:class:`ChatResponse`, :class:`ChatProposal`)
  give the UI a stable contract.  Proposals are generated deterministically
  so the approval gate is reliable offline AND online; configured cloud
  providers power the natural-language discussion around them through the
  existing provider layer.
* Proposal revision accumulates into the SAME proposal across conversation
  turns ("Make OpenCode primary", "Also add notifications"), and approval
  hands ONE exact execution prompt to the existing AutoFix pipeline.
* Self-correction: requests that conflict with the locked architecture
  (second execution engines, bypassing WorkerRouter, Chat executing code)
  are detected and corrected instead of being blindly planned.

Chat NEVER executes project modifications.  Execution happens exclusively
through ApprovalPipeline after explicit user approval.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from app.agents.intent import (
    CODING_CATEGORIES,
    CODING_TASK,
    CONVERSATION as BASE_CONVERSATION,
    DEBUG_TASK,
    PROJECT_MODIFICATION,
    QUESTION,
    classify_intent,
)
from app.agents.large_input import is_large_input

# ----------------------------------------------------------------------
# Web research integration (automatic, non-blocking, provider-agnostic)
# ----------------------------------------------------------------------


def _run_web_research(message: str) -> str:
    """Run automatic web research and return formatted context.

    Returns a context string suitable for injection into the Chat AI
    provider's system context.  Returns empty string on failure or when
    research is not needed.  Never raises exceptions.
    """
    try:
        from app.agents.web_research import research_message

        ctx = research_message(message)
        if ctx.should_research and ctx.context_text:
            return ctx.context_text
    except Exception:
        pass  # research failure must never break the reply
    return ""


# ----------------------------------------------------------------------
# Refined conversational intents (superset of the base classifier)
# ----------------------------------------------------------------------

GREETING = "GREETING"
CONVERSATION = "CONVERSATION"
QUESTION = "QUESTION"
EXPLANATION = "EXPLANATION"
ANALYSIS = "ANALYSIS"
BRAINSTORM = "BRAINSTORM"
RECOMMENDATION = "RECOMMENDATION"
DEBUGGING_DISCUSSION = "DEBUGGING_DISCUSSION"
CODING_REQUEST = "CODING_REQUEST"
PROJECT_REQUEST = "PROJECT_REQUEST"
CHANGE_REQUEST = "CHANGE_REQUEST"
PLAN_REQUEST = "PLAN_REQUEST"
PROPOSAL_REQUEST = "PROPOSAL_REQUEST"
CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"

#: Intents that should end in a structured AutoFix proposal.
PROPOSAL_INTENTS = {
    CODING_REQUEST,
    PROJECT_REQUEST,
    CHANGE_REQUEST,
    PLAN_REQUEST,
    PROPOSAL_REQUEST,
}

_DISCUSSION_INTENTS = {
    GREETING,
    CONVERSATION,
    QUESTION,
    EXPLANATION,
    ANALYSIS,
    BRAINSTORM,
    RECOMMENDATION,
    DEBUGGING_DISCUSSION,
}


@dataclass
class RefinedIntent:
    """Result of the layered conversational classification."""

    category: str
    confidence: float
    base_category: str
    matched: tuple = ()

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"RefinedIntent({self.category}, {self.confidence}, "
            f"base={self.base_category})"
        )


_GREETING_ONLY_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|good\s+(morning|afternoon|evening)|thanks|thank you|"
    r"thx|ty|bye|goodbye|howdy)[\s!.,?]*$",
    re.IGNORECASE,
)

_EXPLANATION_RE = re.compile(
    r"^(explain|describe|define|tell me about|what is|what are|what's|whats|"
    r"how does|how do|how did)\b|\bwork(s)?\??\s*$",
    re.IGNORECASE,
)

_ANALYSIS_RE = re.compile(
    r"^(why|why is|why does|why do|what'?s wrong|what is wrong|analyze|"
    r"investigate|diagnose|walk me through|help me understand why)\b",
    re.IGNORECASE,
)

_BRAINSTORM_RE = re.compile(
    r"\b(brainstorm|ideas? for|options for|alternatives? to|ways to|"
    r"what could we|how might we)\b",
    re.IGNORECASE,
)

_RECOMMENDATION_RE = re.compile(
    r"\b(should we|(which|what) .{0,24}(better|prefer|recommend|choose)|"
    r"recommend|would you suggest|best way|worth it)\b",
    re.IGNORECASE,
)

_DEBUG_DISCUSSION_RE = re.compile(
    r"\b(bug|bugs|error|errors|exception|crash(es|hed|hing)?|fail(s|ed|ing|ure)?|"
    r"broken|not working|doesn'?t work|regression|traceback|stack ?trace)\b",
    re.IGNORECASE,
)

_PLAN_REQUEST_RE = re.compile(
    r"^(how (would|should|could|can) (you|we|i)|can we|could we|"
    r"is it possible to|what would it take to|plan (how|out|for)|draft a plan|"
    r"design an approach)\b",
    re.IGNORECASE,
)

_PROPOSAL_REQUEST_RE = re.compile(
    r"\b(proposal|propose|write up|write-up|rfc|design doc)\b",
    re.IGNORECASE,
)

#: Project-wide scope hints distinguishing PROJECT_REQUEST from CODING_REQUEST.
_PROJECT_SCOPE_RE = re.compile(
    r"\b(project|product|application|app|system|platform|codebase|repo|"
    r"repository|workspace|studio|autofix)\b",
    re.IGNORECASE,
)

_PRONOUN_SUBJECT_RE = re.compile(
    r"^(make|update|improve|fix|change|refactor|optimize|optimise|speed ?up|"
    r"finish|extend|simplify|clean( up)?)\s+(it|this|that|them|these|those)\b",
    re.IGNORECASE,
)

_PRONOUN_ANY_RE = re.compile(r"\b(it|this|that|those|these|them)\b", re.IGNORECASE)

_WORKER_TOKENS = {
    "opencode": ("opencode",),
    "deepseek": ("deepseek",),
    "copilot": ("github copilot", "copilot"),
}
_WORKER_DISPLAY = {"opencode": "OpenCode", "deepseek": "DeepSeek", "copilot": "GitHub Copilot"}
_DEFAULT_PRIORITY = ("opencode", "deepseek", "copilot")

_PRIMARY_ROLE_RE = re.compile(r"\b(primary|first|default|top|before)\b", re.IGNORECASE)
_FALLBACK_ROLE_RE = re.compile(r"\b(fallback|backup|second|after|then|last)\b", re.IGNORECASE)


def classify_conversation_intent(message, history=None, active_proposal=None):
    """Layered conversational classification (delegates to classify_intent).

    The base classifier stays the single authority for coding detection;
    this refinement adds conversational categories without changing any
    existing classification result.
    """
    base = classify_intent(message)
    text = (message or "").strip()
    lowered = text.lower()

    # Explicit proposal/plan language wins immediately.
    if _PLAN_REQUEST_RE.match(lowered):
        return RefinedIntent(PLAN_REQUEST, max(base.confidence, 0.8), base.category, ("plan_phrase",))
    if _PROPOSAL_REQUEST_RE.search(lowered) and not base.is_coding_task:
        return RefinedIntent(PROPOSAL_REQUEST, 0.85, base.category, ("proposal_phrase",))

    if base.is_coding_task:
        # Unresolvable pronoun subjects beat confident verb matching:
        # "Make it faster." alone cannot be planned responsibly.
        recent = [
            (speaker, str(content)[:_MESSAGE_CLIP])
            for speaker, content in (history or [])[-MAX_RECENT_MESSAGES:]
        ]
        if (
            _PRONOUN_SUBJECT_RE.match(text)
            and not ARTIFACT_HINT_RE.search(text)
            and not any(_ARTIFACT_TOPIC_RE.search(c or "") for _s, c in recent)
        ):
            return RefinedIntent(
                CLARIFICATION_REQUIRED, max(base.confidence, 0.75), base.category,
                ("unresolved_pronoun",),
            )
        if _PROJECT_SCOPE_RE.search(lowered):
            return RefinedIntent(PROJECT_REQUEST, base.confidence, base.category, ())
        if base.category == PROJECT_MODIFICATION:
            return RefinedIntent(CHANGE_REQUEST, base.confidence, base.category, ())
        if base.category == DEBUG_TASK:
            return RefinedIntent(CHANGE_REQUEST, base.confidence, base.category, ())
        return RefinedIntent(CODING_REQUEST, base.confidence, base.category, ())

    if base.category == PROJECT_MODIFICATION:
        return RefinedIntent(CHANGE_REQUEST, base.confidence, base.category, ())
    if base.category == DEBUG_TASK:
        return RefinedIntent(CHANGE_REQUEST, base.confidence, base.category, ())

    # Recommendation-shaped questions stay conversational even when they
    # mention technical work ("Should we use SQLite or JSON storage?").
    if _RECOMMENDATION_RE.search(lowered):
        return RefinedIntent(RECOMMENDATION, max(base.confidence, 0.75), base.category, ())

    # Confident-enough coding shapes phrased as questions/discussions
    # ("Can we make AutoFix remember previous fixes?") are planning talk.
    if base.category in CODING_CATEGORIES:
        if _PLAN_REQUEST_RE.match(lowered):
            return RefinedIntent(PLAN_REQUEST, max(base.confidence, 0.75), base.category, ("feasibility",))
        if lowered.endswith("?") and re.match(r"^(can|could|should|would|is)\b", lowered):
            return RefinedIntent(PLAN_REQUEST, max(base.confidence, 0.72), base.category, ("question_plan",))
        if _DEBUG_DISCUSSION_RE.search(lowered):
            return RefinedIntent(DEBUGGING_DISCUSSION, 0.7, base.category, ())
        return RefinedIntent(BRAINSTORM, 0.65, base.category, ())

    if _GREETING_ONLY_RE.match(text):
        return RefinedIntent(GREETING, 0.95, base.category, ("greeting",))

    if base.category == QUESTION:
        if _EXPLANATION_RE.match(lowered):
            return RefinedIntent(EXPLANATION, base.confidence, base.category, ("explain",))
        if _ANALYSIS_RE.match(lowered):
            return RefinedIntent(ANALYSIS, base.confidence, base.category, ("analysis",))
        if _BRAINSTORM_RE.search(lowered):
            return RefinedIntent(BRAINSTORM, base.confidence, base.category, ())
        if _RECOMMENDATION_RE.search(lowered):
            return RefinedIntent(RECOMMENDATION, base.confidence, base.category, ())
        if _DEBUG_DISCUSSION_RE.search(lowered) and not lowered.rstrip().endswith("?"):
            return RefinedIntent(DEBUGGING_DISCUSSION, base.confidence, base.category, ())
        return RefinedIntent(QUESTION, base.confidence, base.category, ())

    if _DEBUG_DISCUSSION_RE.search(lowered):
        return RefinedIntent(DEBUGGING_DISCUSSION, 0.7, base.category, ("debug_talk",))
    if _BRAINSTORM_RE.search(lowered):
        return RefinedIntent(BRAINSTORM, 0.68, base.category, ())
    if _RECOMMENDATION_RE.search(lowered):
        return RefinedIntent(RECOMMENDATION, 0.68, base.category, ())

    # Unresolvable pronoun-subject directives ("Make it faster.") need the
    # surrounding conversation to supply a subject; without one they are
    # genuinely ambiguous → clarification required.
    if _PRONOUN_SUBJECT_RE.match(text):
        recent = [
            (speaker, str(content)[:_MESSAGE_CLIP])
            for speaker, content in (history or [])[-MAX_RECENT_MESSAGES:]
        ]
        if not _ARTIFACT_TOPIC_RE.search(text) and not any(
            _ARTIFACT_TOPIC_RE.search(content or "") for _s, content in recent
        ):
            return RefinedIntent(CLARIFICATION_REQUIRED, 0.8, base.category, ("unresolved_pronoun",))

    return RefinedIntent(CONVERSATION, base.confidence, base.category, ())


# ----------------------------------------------------------------------
# Locked-architecture self-correction (spec section 11)
# ----------------------------------------------------------------------

_ARCH_CONFLICT_PATTERNS = (
    (re.compile(r"\bbulk\b.{0,20}\b(an?\s+|as\s+a[n]?)\s*(separate|own|real|execution)\b|"
                r"\b(make|turn)\s+bulk\s+(an?|into)\b.{0,20}engine\b|"
                r"\bnew\s+bulk\s+execution\s+engines?\b", re.IGNORECASE),
     "Bulk is an input path into AutoFix, not an engine"),
    (re.compile(r"\b(second|new|separate|own|parallel|dedicated)\s+(bulk\s+)?"
                r"(execution\s+)?engine(s)?\b", re.IGNORECASE),
     "a second execution engine would duplicate AutoFix"),
    (re.compile(r"\bnew\s+worker\s+routers?\b|\bsecond\s+worker\s+routers?\b|"
                r"\banother\s+worker\s+routers?\b", re.IGNORECASE),
     "WorkerRouter is the single routing authority"),
    (re.compile(r"\b(bypass|skip|replace)\s+(the\s+)?worker[\s_-]?routers?\b", re.IGNORECASE),
     "WorkerRouter must never be bypassed"),
    (re.compile(r"\b(let|have|make)\s+chat\s+(directly\s+)?(execute|run|modify|edit)\b", re.IGNORECASE),
     "Chat must never execute project modifications directly"),
)

_CONFLICT_PREFIX = "That would conflict with the current AutoFix architecture. "

_CONFLICT_GUIDANCE = (
    "Here's what fits the architecture instead: keep the responsibility "
    "inside the existing component that owns it, and extend that component "
    "with the smallest change that achieves your goal. If you tell me the "
    "underlying problem you're trying to solve, I'll prepare a proper "
    "AutoFix proposal along those lines."
)


def architecture_conflict_note(text):
    """Return a correction note when a request fights the locked design."""
    lowered = (text or "")
    for pattern, reason in _ARCH_CONFLICT_PATTERNS:
        if pattern.search(lowered):
            return (
                f"{_CONFLICT_PREFIX}{reason.capitalize()}. The safer approach "
                "is to extend the existing components inside their current "
                "responsibilities."
            )
    return None


# ----------------------------------------------------------------------
# Context building (relevant slices only — never the whole history)
# ----------------------------------------------------------------------

MAX_RECENT_MESSAGES = 12
MAX_MEMORY_NOTES = 3
MAX_TASK_NOTES = 3
_MESSAGE_CLIP = 600


@dataclass
class ChatContext:
    """Everything relevant for one turn — deliberately small and filtered."""

    recent_messages: list = field(default_factory=list)
    relevant_project_memory: list = field(default_factory=list)
    relevant_tasks: list = field(default_factory=list)
    current_workspace: str = ""
    current_task: str | None = None      # active proposal objective
    active_plan: str | None = None       # active proposal execution prompt
    resolved_topic: str | None = None    # referent resolved from history
    inspected_files: list = field(default_factory=list)
    #: Relevant SHARED guidance (GitHub AI-knowledge repo) — optional, ranked
    #: BELOW project memory; empty unless a knowledge repository is configured.
    shared_guidance: list = field(default_factory=list)
    #: Automatic web/forum research context — populated when the message
    #: would benefit from current external information.  Empty when research
    #: is not needed or fails.  Never contains secrets.
    web_research_context: str = ""
    #: Relevant AI intelligence context (reusable, validated, approved
    #: intelligence from the Intelligence Storage).  Injected when
    #: IntelligenceManager is available and entries match the query.
    intelligence_context: str = ""

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if self.current_workspace:
            try:
                lines.append(f"Workspace: {Path(self.current_workspace).name}")
            except Exception:
                lines.append(f"Workspace: {self.current_workspace}")
        if self.resolved_topic:
            lines.append(f"Current subject: {self.resolved_topic}")
        if self.inspected_files:
            lines.append("Related project files:\n" + "\n".join(f"  {f}" for f in self.inspected_files))
        for record in self.relevant_project_memory:
            title = record.get("title", "")
            content = str(record.get("content", ""))[:200]
            lines.append(f"[memory:{record.get('kind','')}] {title}: {content}")
        for task in self.relevant_tasks:
            lines.append(
                f"[task] {task.get('task_id','')}: {task.get('objective','')} "
                f"(status={task.get('status','')})"
            )
        for entry in self.shared_guidance:
            lines.append(f"[shared] {entry}")
        if self.web_research_context:
            lines.append("[research] Web research completed — current information available")
        if self.intelligence_context:
            lines.append("[intelligence] Relevant AI intelligence available")
        return lines

    def context_block(self) -> str:
        lines = self.summary_lines()
        return "\n".join(lines[:16])


_ARTIFACT_TOPIC_RE = re.compile(
    r"\b(worker[\s_-]?router|pipeline|autofix task|task memory|project memory|"
    r"verif(y|ication)|recovery|orchestrator|chat provider|planner|"
    r"decomposition|subtasks?|launcher|backend api|frontend ui|"
    r"[A-Za-z0-9_.\-]+\.(?:py|js|ts|json|md))\b",
    re.IGNORECASE,
)


def resolve_referent(message, recent_messages, active_proposal=None):
    """Resolve pronoun-style references against the recent conversation.

    Returns a short topic phrase for follow-ups like “How would you do it?”
    or “Make it faster.”, or None when the message names its subject
    explicitly.
    """
    text = (message or "").strip()
    if active_proposal is not None:
        objective = str(active_proposal.get("objective", "")).strip()
        if objective and _PRONOUN_ANY_RE.search(text):
            return objective
    explicit = bool(
        classify_intent(text).referenced_files
        or ARTIFACT_HINT_RE.search(text)
    )
    if explicit:
        return None
    needs_resolution = bool(_PRONOUN_SUBJECT_RE.match(text)) or (
        _PRONOUN_ANY_RE.search(text) and len(text.split()) <= 12
    )
    if not needs_resolution:
        return None
    for speaker, content in reversed(recent_messages or []):
        match = _ARTIFACT_TOPIC_RE.search(content or "")
        if match:
            return match.group(0)
    return None


ARTIFACT_HINT_RE = re.compile(
    r"\b[A-Za-z0-9_.\-]+\.(?:py|js|ts|tsx|json|md|txt)\b|"
    r"\b(worker[\s_-]?router|pipeline|autofix|verifier|launcher|orchestrator)\b",
    re.IGNORECASE,
)

#: Targeted inspection map: topic keywords → known project files.  Used for
#: surgical project awareness (no whole-repo scans).
_PROJECT_MODULE_MAP = (
    (("worker router", "workerrouter", "worker fallback", "failover",
      "worker selection", "worker priority"), "frontend/app/agents/worker_router.py — WorkerRouter (single routing authority)"),
    (("approval", "pipeline", "execution flow", "stage"), "frontend/app/agents/pipeline.py — ApprovalPipeline (plan → approve → execute)"),
    (("task state", "autofixtask", "decomposition", "subtask", "recovery"),
     "frontend/app/agents/autofix_task.py — AutoFixTask (durable task record)"),
    (("memory", "remember", "project memory", "conversation store"),
     "frontend/app/agents/task_memory.py — project memory (.autofix/memory)"),
    (("notification", "authentication notification", "auth warning"),
     "frontend/app/agents/worker_notifications.py — WorkerNotification"),
    (("chat", "assistant", "conversation"), "frontend/app/agents/chat_provider.py — provider abstraction"),
    (("intent", "classify", "routing"), "frontend/app/agents/intent.py — deterministic intent classifier"),
    (("transport", "large input", "big paste"), "frontend/app/agents/task_transport.py — task transport"),
    (("launcher", ".exe", "startup"), "launcher/launcher.py — launcher"),
    (("test", "pytest", "suite"), "tests/ — root regression suite"),
)


def inspect_project_targets(message, workspace):
    """Targeted file awareness for the current request (paths + roles)."""
    lowered = (message or "").lower()
    targets: list[str] = []
    for keywords, description in _PROJECT_MODULE_MAP:
        if any(keyword in lowered for keyword in keywords):
            targets.append(description)
    # Referenced files that actually exist in the workspace get surfaced.
    for name in classify_intent(message).referenced_files[:3]:
        try:
            if (Path(workspace) / name).exists():
                targets.append(f"{name} (in workspace)")
        except OSError:
            continue
    return list(dict.fromkeys(targets))[:5]


def _load_relevant_tasks(workspace, message):
    tasks_dir = Path(workspace) / ".autofix" / "tasks"
    try:
        paths = sorted(tasks_dir.glob("autofix-task-*.json"), reverse=True)[:10]
    except OSError:
        return []
    query_tokens = {
        token for token in re.findall(r"[a-z]{3,}", (message or "").lower())
    }
    found: list[dict] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        objective = str(data.get("original_request", ""))[:140]
        tokens = set(re.findall(r"[a-z]{3,}", objective.lower()))
        overlap = len(query_tokens & tokens)
        found.append({
            "task_id": data.get("task_id", path.stem),
            "objective": objective,
            "status": data.get("status", ""),
            "verified": data.get("verified"),
            "_score": overlap,
        })
    relevant = [t for t in found if t["_score"] > 0]
    relevant.sort(key=lambda t: t["_score"], reverse=True)
    return [
        {k: v for k, v in t.items() if not k.startswith("_")}
        for t in relevant[:MAX_TASK_NOTES]
    ]


def build_chat_context(message, workspace, history=None, active_proposal=None):
    """Assemble the relevant-context slice for one conversational turn."""
    recent = [
        (speaker, str(content)[:_MESSAGE_CLIP])
        for speaker, content in (history or [])[-MAX_RECENT_MESSAGES:]
    ]
    proposal_dict = None
    if isinstance(active_proposal, dict):
        proposal_dict = active_proposal
    elif active_proposal is not None:
        proposal_dict = getattr(active_proposal, "to_dict", lambda: {})()

    resolved = resolve_referent(message, recent, proposal_dict)
    memory = []
    try:
        from app.agents.task_memory import redact_secrets, retrieve_relevant

        for record in retrieve_relevant(workspace, message or "", limit=MAX_MEMORY_NOTES):
            record = dict(record)
            record["content"] = redact_secrets(str(record.get("content", "")))
            memory.append(record)
    except Exception:
        memory = []

    intelligence_ctx = _load_intelligence_context(message, workspace)

    return ChatContext(
        recent_messages=recent,
        relevant_project_memory=memory,
        relevant_tasks=_load_relevant_tasks(workspace, message),
        current_workspace=str(workspace or ""),
        current_task=(proposal_dict or {}).get("objective"),
        active_plan=(proposal_dict or {}).get("execution_prompt"),
        resolved_topic=resolved,
        inspected_files=inspect_project_targets(message, workspace),
        shared_guidance=_shared_guidance_lines(message),
        intelligence_context=intelligence_ctx,
    )


def _shared_guidance_lines(query: str) -> list:
    """Relevant shared-knowledge snippets (empty unless repo configured).

    Shared guidance is optional context: retrieval failures are swallowed and
    unconfigured environments short-circuit before any network call.
    """
    try:
        from app.agents.github_knowledge import retrieve_knowledge

        entries = retrieve_knowledge(query or "", limit=2)
    except Exception:
        return []
    lines = []
    for entry in entries:
        snippet = " ".join(str(entry.body).split())[:240]
        lines.append(f"[{entry.category}] {entry.title}: {snippet}")
    return lines


def _load_intelligence_context(query: str, workspace: str) -> str:
    """Retrieve relevant intelligence context for the current conversation.

    Returns a formatted string with relevant AI intelligence entries from
    the Intelligence Storage.  Empty string when no relevant intelligence
    is found or when IntelligenceManager is unavailable.  Failures are
    swallowed -- intelligence context is optional enrichment.
    """
    try:
        from app.agents.intelligence_manager import IntelligenceManager

        mgr = IntelligenceManager(workspace)
        return mgr.build_intelligence_context(query, max_chars=1500)
    except Exception:
        return ""


# ----------------------------------------------------------------------
# Structured models (spec section 16)
# ----------------------------------------------------------------------


@dataclass
class ChatProposal:
    objective: str
    understanding: str
    analysis_summary: str
    plan: list = field(default_factory=list)
    affected_components: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    verification_plan: list = field(default_factory=list)
    execution_prompt: str = ""
    status: str = "AWAITING APPROVAL"
    origin_request: str = ""
    revisions: list = field(default_factory=list)
    worker_preference: str = ""

    def to_dict(self) -> dict:
        return {
            "objective": self.objective,
            "understanding": self.understanding,
            "analysis_summary": self.analysis_summary,
            "plan": list(self.plan),
            "affected_components": list(self.affected_components),
            "dependencies": list(self.dependencies),
            "risks": list(self.risks),
            "verification_plan": list(self.verification_plan),
            "execution_prompt": self.execution_prompt,
            "status": self.status,
            "origin_request": self.origin_request,
            "revisions": list(self.revisions),
            "worker_preference": self.worker_preference,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChatProposal":
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**payload)


@dataclass
class ChatResponse:
    content: str
    intent: str
    requires_clarification: bool = False
    clarification: str = ""
    proposal: ChatProposal | None = None
    execution_prompt: str = ""
    confidence: float = 0.0
    context_used: list = field(default_factory=list)
    kind: str = "reply"          # reply | proposal | revision | clarification | approval
    original_request: str = ""
    #: Optional reusable-knowledge candidate awaiting user approval — the UI
    #: shows a NON-BLOCKING Review/Save-to-GitHub/Ignore notification.  The
    #: engine never saves anything itself.
    knowledge_proposal: dict | None = None

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "intent": self.intent,
            "requires_clarification": self.requires_clarification,
            "clarification": self.clarification,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "execution_prompt": self.execution_prompt,
            "confidence": self.confidence,
            "context_used": list(self.context_used),
            "kind": self.kind,
            "original_request": self.original_request,
            "knowledge_proposal": self.knowledge_proposal,
        }


def render_proposal_text(proposal: ChatProposal) -> str:
    """Plain-text AUTOFIX PROPOSAL block (card body + persisted plan)."""
    def section(title, items, numbered=False):
        if not items:
            return ""
        rows = [
            f"{i}. {item}" if numbered else f"- {item}"
            for i, item in enumerate(items, start=1)
        ]
        return f"{title}:\n" + "\n".join(rows)

    parts = ["AUTOFIX PROPOSAL", "", f"Objective:\n{proposal.objective}"]
    if proposal.understanding:
        parts.append(f"Understanding:\n{proposal.understanding}")
    if proposal.analysis_summary:
        parts.append(f"Current Architecture / Analysis:\n{proposal.analysis_summary}")
    body = "\n\n".join(parts)
    rest = "\n\n".join(filter(None, [
        section("Implementation Plan", proposal.plan, numbered=True),
        section("Files / Components", proposal.affected_components),
        section("Dependencies", proposal.dependencies),
        section("Risks", proposal.risks),
        section("Verification", proposal.verification_plan, numbered=True),
    ]))
    text = body + ("\n\n" + rest if rest else "")
    text += f"\n\nStatus:\n{proposal.status}"
    return text


# ----------------------------------------------------------------------
# Approval / revision utterance detection
# ----------------------------------------------------------------------

_APPROVAL_STRICT_RE = re.compile(
    r"^(approve|approved|approve & execute|approve and execute|approve it|"
    r"approve the (plan|proposal)|execute it|run it|do it|go ahead|go ahead and (run|execute)|"
    r"lgtm|ship it|that works|looks good|sounds good|yes please|yes, proceed|"
    r"proceed|confirmed)[\s!.]*$",
    re.IGNORECASE,
)

_DIRECTIVE_START_RE = re.compile(
    r"^(make|set|use|switch|prefer|add|also|include|plus|remove|drop|change|"
    r"rename|instead|update|revise|adjust|keep|without)\b",
    re.IGNORECASE,
)


def is_approval_message(message) -> bool:
    text = (message or "").strip()
    if not text or len(text.split()) > 8:
        return False
    return bool(_APPROVAL_STRICT_RE.match(text))


def is_revision_message(message, refined=None) -> bool:
    text = (message or "").strip()
    if not text or is_approval_message(text):
        return False
    if refined is None:
        refined = classify_conversation_intent(text)
    if refined.category in PROPOSAL_INTENTS:
        return True
    if refined.base_category in CODING_CATEGORIES:
        return True
    return bool(_DIRECTIVE_START_RE.match(text)) and len(text.split()) <= 40


# ----------------------------------------------------------------------
# Clarification rules — ONLY when missing info materially changes the result
# ----------------------------------------------------------------------

_CLARIFICATION_RULES = (
    (
        re.compile(r"\bconnect\b.{0,40}\b(my|our|the)\b.{0,20}\bapi\b", re.IGNORECASE),
        "Which API or service should AutoFix connect to? A base URL plus the "
        "auth style (API key or OAuth) is enough for me to draft the integration.",
    ),
    (
        re.compile(r"\bintegrate\b.{0,30}\b(database|db)\b", re.IGNORECASE),
        "Which database engine should we target (SQLite, PostgreSQL, MySQL)?",
    ),
    (
        re.compile(r"\bdeploy\b.{0,30}\b(my|our|the)\b.{0,20}(app|application|project)\b",
                   re.IGNORECASE),
        "Where should this deploy (target platform/environment)?",
    ),
)


def _mentions_explicit_target(text):
    """True when the message already names its integration target."""
    lowered = (text or "").lower()
    return bool(
        re.search(
            r"\b(https?://|www\.|rest|graphql|grpc|soap|webhook|oauth2?|"
            r"api[- ]?key)\b",
            lowered,
        )
        or re.search(
            r"\b(sqlite|postgres(?:ql)?|mysql|mariadb|mongodb|mssql|redis)\b",
            lowered,
        )
    )


def clarification_question(message, ctx):
    """A materially necessary clarifying question, or None."""
    text = (message or "").strip()
    for pattern, question in _CLARIFICATION_RULES:
        if pattern.search(text):
            return question
    # Unresolvable pronoun subject with no conversation to resolve against.
    if _PRONOUN_SUBJECT_RE.match(text) and not ctx.resolved_topic and not ctx.active_plan:
        return (
            "Which part of the project should I apply that to? Name the "
            "component or feature and I'll prepare a concrete proposal."
        )
    return None


# ----------------------------------------------------------------------
# Deterministic proposal generation & revision
# ----------------------------------------------------------------------

_VERIFICATION_STEPS = [
    "python -m compileall frontend/app",
    "Run the root test suite (pytest -q)",
    "Run backend and frontend suites",
]


def _clean_objective(request):
    text = re.sub(r"\s+", " ", (request or "")).strip().rstrip(".?")
    text = re.sub(
        r"^(please\s+|pls\s+|plz\s+|can you\s+|could you\s+|would you\s+|"
        r"can we\s+|could we\s+|i want you to\s+|i need you to\s+|"
        r"i(')?( would)? (like|want|need) (to|you to)\s+|we (want|need) to\s+|"
        r"let'?s\s+|how (about|would|should) (you|we)\s+)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if not text:
        text = (request or "").strip()
    return text or "Requested change"


def _kind_from_intent(refined):
    if refined.category in (CHANGE_REQUEST, PLAN_REQUEST, PROPOSAL_REQUEST):
        return "change"
    return "create"


def generate_proposal(message, ctx, refined=None):
    """Deterministic, high-quality structured proposal for one request."""
    refined = refined or classify_conversation_intent(message)
    kind = _kind_from_intent(refined)
    objective = _clean_objective(message)
    components = list(ctx.inspected_files) or [
        "Affected files identified during execution (workspace-aware)"
    ]

    understanding = f"You want to {_lower_first(objective)}."
    if ctx.resolved_topic:
        understanding += (
            f" This builds on the current subject of our conversation: "
            f"{ctx.resolved_topic}."
        )

    analysis_bits: list[str] = []
    if ctx.inspected_files:
        analysis_bits.append(
            "Relevant existing components:\n" +
            "\n".join(f"- {c}" for c in ctx.inspected_files)
        )
    if ctx.relevant_tasks:
        analysis_bits.append(
            "Related past tasks: "
            + "; ".join(f"{t['objective']} ({t['status']})" for t in ctx.relevant_tasks)
        )
    analysis_summary = "\n".join(analysis_bits) or (
        "The active workspace was inspected; implementation details will be "
        "resolved against the actual project structure during execution."
    )

    if kind == "create":
        plan = [
            f"Inspect the workspace and confirm conventions for: {objective}",
            f"Implement: {objective}",
            "Add or extend tests covering the new behavior",
            "Run verification and confirm everything passes",
        ]
        risks = [
            "New code may interact with existing modules — mitigated by running all suites",
            "Scope creep beyond the stated objective — execution is limited to the requirements below",
        ]
    elif kind == "change":
        plan = [
            f"Locate the code affected by: {objective}",
            f"Implement the change: {objective}",
            "Update or extend the relevant tests",
            "Run the full verification pass",
        ]
        risks = [
            "Regression risk in touched components — mitigated by the existing test suites",
            "Locked architecture must be preserved (no duplicated routers/engines)",
        ]
    else:  # pragma: no cover - defensive
        plan = [f"Implement: {objective}", "Verify the result"]
        risks = []

    verification = list(_VERIFICATION_STEPS)

    proposal = ChatProposal(
        objective=objective,
        understanding=understanding,
        analysis_summary=analysis_summary,
        plan=plan,
        affected_components=components,
        dependencies=["No new third-party dependencies required"],
        risks=risks,
        verification_plan=verification,
        status="AWAITING APPROVAL",
        origin_request=message,
    )
    proposal.execution_prompt = build_execution_prompt(proposal)
    return proposal


def _lower_first(text):
    return text[:1].lower() + text[1:] if text else text


def build_execution_prompt(proposal: ChatProposal) -> str:
    """The complete, self-contained prompt AutoFix will execute verbatim."""
    requirement_lines = []
    for i, step in enumerate(proposal.plan, start=1):
        requirement_lines.append(f"{i}. {step}")
    constraint_lines = [
        "Preserve the locked AutoFix architecture: Chat discusses/plans, "
        "ApprovalPipeline executes, WorkerRouter routes, internal workers "
        "(OpenCode/DeepSeek/Copilot) execute, verification gates completion.",
        "Do not create duplicate routers, task systems or execution engines.",
        "Never hard-code credentials or log secrets.",
    ]
    if proposal.worker_preference:
        constraint_lines.append(f"Worker preference: {proposal.worker_preference}.")
    sections = [
        f"OBJECTIVE\n{proposal.objective}",
        "CONTEXT\n"
        + (proposal.understanding or "Approved via the AutoFix Chat proposal flow.")
        + (f"\n\n{proposal.analysis_summary}" if proposal.analysis_summary else ""),
        "REQUIREMENTS\n" + "\n".join(requirement_lines),
        "CONSTRAINTS\n" + "\n".join(f"- {line}" for line in constraint_lines),
        "VERIFICATION\n" + "\n".join(f"- {step}" for step in proposal.verification_plan),
    ]
    return "\n\n".join(sections)


def _mentioned_workers(text):
    lowered = (text or "").lower()
    mentioned: list[str] = []
    for worker, tokens in _WORKER_TOKENS.items():
        for token in tokens:
            idx = lowered.find(token)
            if idx >= 0:
                mentioned.append((idx, worker))
                break
    return [w for _, w in sorted(mentioned)]


def apply_worker_priority(message):
    """Parse worker-priority directives → ('opencode > deepseek', …) or ''."""
    workers = _mentioned_workers(message)
    if not workers:
        return ""
    has_fallback_word = bool(_FALLBACK_ROLE_RE.search(message))
    has_primary_word = bool(_PRIMARY_ROLE_RE.search(message))
    if not (has_primary_word or has_fallback_word):
        return ""
    ordering = list(workers)
    for w in _DEFAULT_PRIORITY:
        if w not in ordering:
            ordering.append(w)
    names = " → ".join(_WORKER_DISPLAY[w] for w in ordering)
    return f"{names} (priority order)"


def apply_revision(proposal: ChatProposal, message, ctx=None) -> ChatProposal:
    """Accumulate one conversational revision into the SAME proposal."""
    updated = replace(
        proposal,
        plan=list(proposal.plan),
        affected_components=list(proposal.affected_components),
        dependencies=list(proposal.dependencies),
        risks=list(list(proposal.risks)),
        verification_plan=list(proposal.verification_plan),
        revisions=list(proposal.revisions),
    )

    # Worker priority directives.
    preference = apply_worker_priority(message)
    if preference:
        updated.worker_preference = preference
        updated.plan.append(f"Set worker routing priority: {preference}.")
        updated.risks = [
            r for r in updated.risks
            if "worker" not in r.lower() or "priority" in r.lower()
        ]

    # Additive clauses ("also add X", "and add Y").
    for match in re.finditer(
        r"\b(?:also\s+|and\s+|plus\s+)?add\s+(?P<clause>[^.;\n]{4,160})", message, re.IGNORECASE
    ):
        clause = match.group("clause").strip().rstrip(",.")
        if not clause:
            continue
        step = f"Add: {clause[0].upper()}{clause[1:]}"
        if step not in updated.plan:
            updated.plan.insert(len(updated.plan) - 1, step)
        verify_step = f"Verify: {clause}"
        if verify_step not in updated.verification_plan:
            updated.verification_plan.insert(len(updated.verification_plan) - 1, verify_step)

    # Removals.
    for match in re.finditer(
        r"\b(?:remove|drop|without)\s+(?P<clause>[^.;\n]{4,160})", message, re.IGNORECASE
    ):
        clause = match.group("clause").strip().rstrip(",.")
        updated.plan = [
            s for s in updated.plan if clause.lower() not in s.lower()
        ]
        updated.plan.append(f"Scope adjustment: exclude {clause}.")
        break

    updated.revisions.append(_clean_objective(message))
    updated.understanding = (
        f"You want to {_lower_first(updated.objective)}."
        + (f" Accumulated revisions: {len(updated.revisions)}." if updated.revisions else "")
    )
    updated.origin_request = message if not updated.origin_request else updated.origin_request
    updated.status = "AWAITING APPROVAL"
    updated.execution_prompt = build_execution_prompt(updated)
    return updated


# ----------------------------------------------------------------------
# Discussion replies (offline-coherent, provider-backed when configured)
# ----------------------------------------------------------------------

_DISCUSSION_LEADS = {
    ANALYSIS: (
        "Here's my read of that:\n\n"
        "- Most likely cause: the behavior you describe usually traces back "
        "to the component that owns this responsibility.\n"
    ),
    DEBUGGING_DISCUSSION: (
        "That sounds like something worth pinning down precisely before "
        "changing anything.\n\n"
        "- Typical causes for this class of failure: state carried across "
        "runs, environment differences, or an assumption in the owning "
        "module that no longer holds.\n"
    ),
    BRAINSTORM: (
        "A few directions worth considering:\n"
        "1. The smallest change that solves the immediate problem.\n"
        "2. A slightly broader refactor if the same pattern recurs elsewhere.\n"
        "3. A structural option only if the first two prove insufficient.\n"
    ),
    RECOMMENDATION: (
        "My recommendation: prefer the option that keeps responsibilities "
        "where they already live today — fewer moving parts, easier testing, "
        "and the existing suites keep guarding the behavior.\n"
    ),
}


def _discussion_reply(message, ctx, refined):
    lead = _DISCUSSION_LEADS.get(refined.category)
    if lead is None:
        return None
    body = lead
    if ctx.resolved_topic:
        body += f"\nIn this case the subject is: {ctx.resolved_topic}."
    if ctx.inspected_files:
        body += "\nRelevant places to look:\n" + "\n".join(
            f"- {f}" for f in ctx.inspected_files
        )
    body += (
        "\n\nIf you'd like, I can turn this into a concrete AutoFix "
        "proposal — nothing gets executed without your approval."
    )
    return body


def _contextual_followup_intro(ctx):
    if ctx.resolved_topic:
        return f"Building on {ctx.resolved_topic}: "
    return ""


# ----------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------


class ChatEngine:
    """One conversational brain used by ChatWorker (thread-safe by design).

    Stateless across calls: every ``handle`` receives the conversation tail,
    the active proposal (if any) and the workspace, and returns one
    :class:`ChatResponse`.  Cloud providers are used for natural discussion;
    proposals/approval gating stay deterministic.  Reusable-knowledge
    detection runs after the response is assembled — detection only ATTACHES
    a candidate for user review; it never saves or executes anything.
    """

    #: Process-lifetime dedupe so repeated insights don't re-trigger the
    #: knowledge notification (bounded, deterministic).
    _seen_knowledge_titles: set = set()

    def handle(self, message, workspace, history=None, active_proposal=None, env=None):
        response = self._handle_impl(message, workspace, history, active_proposal, env)
        try:
            self._attach_knowledge(response, message)
        except Exception:
            pass  # knowledge discovery must never break a reply
        return response

    # -- Reusable knowledge discovery (Part 5/6) ------------------------

    _SEEN_KNOWLEDGE_LIMIT = 50

    def _attach_knowledge(self, response, message):
        if getattr(response, "knowledge_proposal", None):
            return
        if response.kind not in ("reply", "proposal", "revision"):
            return
        from app.agents.knowledge_detection import (
            detect_reusable_knowledge,
            normalize_title,
        )

        candidate = detect_reusable_knowledge(
            message or "",
            response.content or "",
            source_context=f"Chat conversation ({response.intent})",
        )
        if candidate is None:
            return
        key = normalize_title(candidate.title)
        if not key or key in ChatEngine._seen_knowledge_titles:
            return
        ChatEngine._seen_knowledge_titles.add(key)
        if len(ChatEngine._seen_knowledge_titles) > ChatEngine._SEEN_KNOWLEDGE_LIMIT:
            ChatEngine._seen_knowledge_titles = set(
                sorted(ChatEngine._seen_knowledge_titles)[
                    -ChatEngine._SEEN_KNOWLEDGE_LIMIT:
                ]
            )
        response.knowledge_proposal = candidate.to_dict()

    def _handle_impl(self, message, workspace, history=None, active_proposal=None, env=None):
        text = (message or "").strip()
        ctx = build_chat_context(text, workspace, history, active_proposal)
        conflict = architecture_conflict_note(text)

        proposal_dict = None
        if isinstance(active_proposal, dict):
            proposal_dict = active_proposal
        elif active_proposal is not None:
            proposal_dict = getattr(active_proposal, "to_dict", lambda: {})()

        # ── Active proposal session: approvals & revisions ────────────
        if proposal_dict is not None:
            if is_approval_message(text):
                origin = proposal_dict.get("origin_request") or text
                return ChatResponse(
                    content=(
                        "Approved. Handing the final proposal to AutoFix — "
                        "one task will be created and executed through the "
                        "existing pipeline."
                    ),
                    intent="APPROVAL",
                    confidence=0.95,
                    execution_prompt=proposal_dict.get("execution_prompt") or "",
                    context_used=ctx.summary_lines(),
                    kind="approval",
                    original_request=origin,
                )

            refined = classify_conversation_intent(text, history, proposal_dict)
            if is_revision_message(text, refined):
                proposal = ChatProposal.from_dict(proposal_dict)
                revised = apply_revision(proposal, text, ctx)
                changed = (
                    revised.execution_prompt != proposal.execution_prompt
                    or revised.revisions != []
                )
                if changed:
                    prefix = f"{conflict}\n\n" if conflict else ""
                    return ChatResponse(
                        content=(
                            prefix
                            + f"Updated the proposal (revision {len(revised.revisions)}). "
                            "It still awaits your approval."
                        ),
                        intent=refined.category,
                        confidence=max(refined.confidence, 0.8),
                        proposal=revised,
                        execution_prompt=revised.execution_prompt,
                        context_used=ctx.summary_lines(),
                        kind="revision",
                        original_request=text,
                    )
                # No concrete delta — treat as a question about the proposal
                # and answer conversationally below.
            # Questions ABOUT the pending proposal fall through to normal
            # conversation (with proposal context attached).

        # ── Clarification gate (materially necessary only) ─────────────
        refined = classify_conversation_intent(text, history, proposal_dict)
        # Explicit ambiguity rules fire regardless of the conversational
        # category ("Connect AutoFix to my API." reads like conversation but
        # cannot be planned without knowing WHICH API).
        rule_question = clarification_question(text, ctx)
        if (
            rule_question is not None
            and refined.category not in _DISCUSSION_INTENTS | {CLARIFICATION_REQUIRED}
            and not _mentions_explicit_target(text)
        ):
            return ChatResponse(
                content=rule_question,
                intent=CLARIFICATION_REQUIRED,
                requires_clarification=True,
                clarification=rule_question,
                confidence=0.85,
                context_used=ctx.summary_lines(),
                kind="clarification",
                original_request=text,
            )
        if refined.category == CLARIFICATION_REQUIRED:
            return ChatResponse(
                content=rule_question or "Could you tell me which component this applies to?",
                intent=CLARIFICATION_REQUIRED,
                requires_clarification=True,
                clarification=rule_question or "",
                confidence=0.85,
                context_used=ctx.summary_lines(),
                kind="clarification",
                original_request=text,
            )
        # Pronoun-subject directives with nothing to resolve against
        # ("Make it faster." as the first message) are genuinely ambiguous.
        if (
            proposal_dict is None
            and not ctx.resolved_topic
            and refined.base_category in CODING_CATEGORIES
            and _PRONOUN_SUBJECT_RE.match(text)
        ):
            return ChatResponse(
                content=(
                    "Which part of the project should I apply that to? Name "
                    "the component or feature and I'll prepare a concrete "
                    "proposal."
                ),
                intent=CLARIFICATION_REQUIRED,
                requires_clarification=True,
                confidence=0.85,
                context_used=ctx.summary_lines(),
                kind="clarification",
                original_request=text,
            )

        # ── Automatic web research (non-blocking, provider-agnostic) ──
        # Research runs for discussion/question intents and for coding
        # requests (to enrich proposal analysis).  It never runs for
        # greetings, clarifications, or active proposal revisions.
        if (
            refined.category not in (GREETING, CLARIFICATION_REQUIRED)
            and proposal_dict is None
        ):
            try:
                research_ctx = _run_web_research(text)
                if research_ctx:
                    ctx.web_research_context = research_ctx
            except Exception:
                pass  # research must never block the reply

        # ── Proposal-worthy intents ────────────────────────────────────
        if refined.category in PROPOSAL_INTENTS or refined.base_category in (
            DEBUG_TASK, PROJECT_MODIFICATION
        ) or refined.base_category == CODING_TASK:
            # Architecture conflicts are corrected conversationally — Chat
            # never drafts a proposal that violates the locked design.
            if conflict:
                return ChatResponse(
                    content=f"{conflict}\n\n{_CONFLICT_GUIDANCE}",
                    intent=refined.category,
                    confidence=0.9,
                    context_used=ctx.summary_lines(),
                    kind="reply",
                    original_request=text,
                )
            proposal = generate_proposal(text, ctx, refined)
            content = (
                "Here's what I propose.\n\n"
                + f"Objective: {proposal.objective}\n"
                + "Review the proposal card below — nothing runs until you "
                "press APPROVE & EXECUTE. You can also just tell me what to "
                "change and I'll revise it."
            )
            return ChatResponse(
                content=content,
                intent=refined.category,
                confidence=max(refined.confidence, 0.8),
                proposal=proposal,
                execution_prompt=proposal.execution_prompt,
                context_used=ctx.summary_lines(),
                kind="proposal",
                original_request=text,
            )

        # ── Plain conversation / discussion ─────────────────────────────
        reply = self._conversational_reply(text, workspace, ctx, refined, history, env)
        prefix = f"{conflict}\n\n" if conflict else ""
        return ChatResponse(
            content=prefix + reply,
            intent=refined.category,
            confidence=refined.confidence,
            context_used=ctx.summary_lines(),
            kind="reply",
            original_request=text,
        )

    def _conversational_reply(self, text, workspace, ctx, refined, history, env):
        # Discussion-shaped intents get a deterministic scaffold offline;
        # cloud providers (when configured) still enrich plain conversation.
        # Web research context (when available) is injected to give the
        # provider current external information for synthesis.
        from app.agents.chat_provider import available_providers, converse

        contextual_intro = _contextual_followup_intro(ctx)
        scaffold = _discussion_reply(text, ctx, refined)
        providers = available_providers(env)
        if providers:
            history_tail = [
                (speaker, content[-400:])
                for speaker, content in (history or [])[-10:]
            ]
            # Build system context: existing project context + web research + intelligence
            system_parts = [ctx.context_block()]
            if ctx.web_research_context:
                system_parts.append(ctx.web_research_context)
            if ctx.intelligence_context:
                system_parts.append(ctx.intelligence_context)
            combined_context = "\n\n".join(filter(None, system_parts))
            try:
                reply = converse(
                    (contextual_intro + text) if contextual_intro else text,
                    workspace,
                    history=history_tail,
                    env=env,
                    system_context=combined_context,
                )
                if reply:
                    if scaffold and refined.category in (ANALYSIS, DEBUGGING_DISCUSSION):
                        return reply + "\n\n" + scaffold.split("\n\n", 1)[-1]
                    return reply
            except Exception:
                pass  # fall back to local scaffolds below — never dead-end
        if scaffold:
            return scaffold
        return converse(text, workspace, history=[], env={})