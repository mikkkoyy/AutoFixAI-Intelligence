"""Deterministic coding-intent classification.

Pure, offline, rule-based classification of a chat message into one of:

    CONVERSATION           — greetings, small talk, thanks.
    QUESTION               — informational questions ("what is React?").
    CODING_TASK            — create/build something new in the project.
    DEBUG_TASK             — fix/debug a defect ("fix this error").
    PROJECT_MODIFICATION   — change what exists ("add dark mode").

The classifier never touches the network and never depends on an AI
provider.  It does NOT route messages anywhere: Chat mode always talks to
the configured Chat AI provider and never transfers anything to AutoFix.
The only consumer is ``LocalAssistant`` in ``app.agents.chat_provider``,
which uses the category to phrase its offline answers (knowledge-base
lookup for questions, a "switch to AutoFix mode" hint for coding tasks).

Confidence model: an imperative action verb at the start of the message
(after stripping greetings/politeness) is a strong signal; software
artifacts, file references and object pronouns raise confidence further.
A message only counts as a coding task when its confidence reaches
``HANDOFF_THRESHOLD``.
"""

from __future__ import annotations

import re

from app.agents.large_input import is_large_input

CONVERSATION = "CONVERSATION"
QUESTION = "QUESTION"
CODING_TASK = "CODING_TASK"
DEBUG_TASK = "DEBUG_TASK"
PROJECT_MODIFICATION = "PROJECT_MODIFICATION"

#: Categories that mean "this requires project modification".
CODING_CATEGORIES = (CODING_TASK, DEBUG_TASK, PROJECT_MODIFICATION)

#: Minimum confidence for a result to count as a coding task.
HANDOFF_THRESHOLD = 0.70


class IntentResult:
    """Outcome of classifying one chat message."""

    __slots__ = ("category", "confidence", "matched", "referenced_files")

    def __init__(self, category, confidence, matched=(), referenced_files=()):
        self.category = category
        self.confidence = round(float(confidence), 2)
        self.matched = tuple(matched)
        self.referenced_files = tuple(referenced_files)

    @property
    def is_coding_task(self) -> bool:
        return self.category in CODING_CATEGORIES and self.confidence >= HANDOFF_THRESHOLD

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"IntentResult({self.category}, {self.confidence}, "
            f"matched={self.matched})"
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, IntentResult):
            return NotImplemented
        return (
            self.category == other.category
            and self.confidence == other.confidence
        )

    def __hash__(self) -> int:  # pragma: no cover
        return hash((self.category, self.confidence))


# ----------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------

_GREETING_TOKENS = {
    "hi", "hello", "hey", "yo", "thanks", "thank", "thx", "ty", "ok",
    "okay", "alright", "sorry", "goodbye", "bye", "welcome", "cool",
    "nice", "great", "awesome", "perfect",
}

_POLITENESS_PREFIXES = (
    "please", "pls", "plz", "kindly",
    "can you", "could you", "would you", "will you", "can we", "could we",
    "i want you to", "i need you to", "i would like you to",
    "i'd like you to", "i want to", "i need to", "i would like to",
    "i'd like to", "i wanna", "we want to", "we need to",
    "i want", "i need", "i would like", "i'd like",
    "help me", "help us", "let's", "lets", "try to",
)

_FILLER_PREFIXES = _GREETING_TOKENS | {"so", "well", "um", "uh", "now"}

#: Sentence starters that make a message informational.  "how about …" /
#: "what about …" are suggestions, not questions, so they are excluded and
#: fall through to verb detection.
_INFO_STARTERS = (
    "what ", "what's ", "whats ", "whats", "who ", "who's ", "whose ",
    "why ", "when ", "where ", "which ", "how ", "is ", "are ", "am ",
    "was ", "were ", "do ", "does ", "did ", "should ", "shall ", "may ",
    "might ", "must ", "tell me ", "explain", "describe ", "define ",
    "meaning of", "difference between", "guide me on", "advise ",
    "understand", "clarify",
)

_BUILD_VERBS = {
    "create", "creates", "created", "creating",
    "build", "builds", "building", "built",
    "make", "makes", "making", "made",
    "generate", "generates", "generated", "generating",
    "write", "writes", "writing", "written",
    "develop", "develops", "developed", "developing",
    "scaffold", "scaffolding", "design", "designs", "designing", "designed",
    "setup",
}

_DEBUG_VERBS = {
    "fix", "fixes", "fixed", "fixing",
    "debug", "debugs", "debugged", "debugging",
    "repair", "repairs", "repaired", "repairing",
    "resolve", "resolves", "resolved", "resolving",
    "correct", "corrects", "corrected", "correcting",
    "patch", "patches", "patched", "patching",
    "troubleshoot", "troubleshooting", "troubleshot",
    "diagnose", "diagnosed", "diagnosing",
}

_MODIFY_VERBS = {
    "add", "adds", "added", "adding",
    "implement", "implements", "implemented", "implementing",
    "integrate", "integrates", "integrated", "integrating",
    "connect", "connects", "connected", "connecting",
    "insert", "include", "includes", "including",
    "support", "supports", "supporting",
    "enable", "enables", "enabling",
    "refactor", "refactors", "refactored", "refactoring",
    "modify", "modifies", "modified", "modifying",
    "update", "updates", "updated", "updating",
    "edit", "edits", "edited", "editing",
    "change", "changes", "changed", "changing",
    "rename", "renames", "renamed", "renaming",
    "remove", "removes", "removed", "removing",
    "delete", "deletes", "deleted", "deleting",
    "replace", "replaces", "replaced", "replacing",
    "optimize", "optimizes", "optimized", "optimizing",
    "optimise", "optimises", "optimised", "optimising",
    "improve", "improves", "improved", "improving",
    "cleanup", "clean up", "extend", "extends", "extended", "extending",
    "migrate", "migrates", "migrated", "migrating",
    "convert", "converts", "converted", "converting",
    "restructure", "restructured", "restructuring",
    "reorganize", "reorganise", "simplify", "simplified",
    "adjust", "adjusts", "adjusted", "adjusting",
    "rewrite", "rewrites", "rewrote", "rewriting",
}

_ALL_VERB_FORMS = _BUILD_VERBS | _DEBUG_VERBS | _MODIFY_VERBS

_DEBUG_NOUNS = re.compile(
    r"\b(bug|bugs|error|errors|exception|exceptions|crash|crashes|crashed|"
    r"failure|failures|failing|fails|failed|broken|regression|traceback|"
    r"stack ?trace|warning|warnings|deprecation|not working|doesn'?t work)\b"
)

_ARTIFACT_RE = re.compile(
    r"\b(page|pages|website|web ?site|web ?app|webapp|site|app|apps|application|"
    r"dashboard|calculator|component|components|widget|module|modules|package|"
    r"library|function|functions|method|methods|class|classes|script|scripts|"
    r"service|services|endpoint|endpoints|api|apis|crud|form|forms|button|buttons|"
    r"menu|navbar|navigation|sidebar|header|footer|modal|modals|dialog|popup|"
    r"table|tables|grid|layout|layouts|theme|themes|style|styles|stylesheet|css|"
    r"sass|scss|tailwind|bootstrap|animation|animations|transition|transitions|"
    r"dark mode|light mode|theme switcher|login|log ?in|login page|sign ?up|signup|"
    r"registration|register|authentication|authenticator|auth|authorization|oauth|"
    r"jwt|session|sessions|cookie|cookies|database|db|schema|migrations?|model|"
    r"models|query|queries|sql|postgres|mysql|sqlite|mongodb|redis|cache|caching|"
    r"config|configuration|settings?|preferences?|feature|features|screen|screens|"
    r"view|views|controller|controllers|router|routers|route|routes|middleware|"
    r"template|templates|tests?|unit ?tests?|integration ?tests?|pytest|readme|docs?"
    r"|documentation|cli|bot|bots|game|games|player|editor|viewer|converter|parser|"
    r"compiler|interpreter|algorithm|regex|validation|logging|deployment|pipeline|"
    r"ci/?cd|docker|kubernetes|react|vue|angular|svelte|next\.js|nuxt|django|flask|"
    r"fastapi|express|node|node\.js|npm|pyside|pyqt|tkinter|frontend|front ?end|"
    r"backend|back ?end|fullstack|ui|ux|gui|responsive|landing page|portfolio|blog|"
    r"forum|chat|chatbot|messenger|dashboard widget|component library|helper|utils?)\b"
)

_FILE_EXT_RE = re.compile(
    r"\b[\w][\w\-\.]*\.(?:py|js|ts|tsx|jsx|html?|css|scss|sass|less|json|ya?ml|"
    r"xml|md|markdown|txt|sh|bash|bat|ps1|psm1|sql|toml|ini|cfg|conf|env|go|rs|rb|"
    r"php|java|kt|kts|swift|m|mm|c|h|cpp|hpp|cc|cs|fs|vb|r|jl|lua|dart|vue|svelte|"
    r"gradle|lock|csv|ipynb)\b"
)

_THIS_CODEBASE_RE = re.compile(
    r"\b(this|the current|current|my|our|the) (file|module|project|codebase|repository|repo|code|program|app|application|workspace|folder|directory|package|library)\b|\bthis code\b"
)

_PRONOUN_OBJECT_RE = re.compile(r"\b(it|this|that|these|those|them|here)\b")

_POSSESSIVE_RE = re.compile(r"\b(my|our|current|existing)\b")

#: Tokens that, when they directly precede a verb, mean it is NOT an
#: imperative directed at the assistant ("the create endpoint", "they fixed").
_SUBJECT_GUARD = {
    "the", "a", "an", "this", "that", "these", "those", "my", "our", "your",
    "his", "her", "its", "their", "is", "are", "was", "were", "be", "been",
    "being", "new", "another", "any", "some", "each", "every", "i", "you",
    "he", "she", "it", "we", "they", "there", "has", "have", "had",
    "after", "before", "while", "when", "if", "about", "of", "for", "to",
    "from", "with", "without", "by", "at", "on", "in",
}


# ----------------------------------------------------------------------
# Normalisation helpers
# ----------------------------------------------------------------------

def _normalize(message: str) -> tuple[str, list[str]]:
    """Return (normalized_text, tokens)."""
    text = re.sub(r"\s+", " ", (message or "")).strip()
    tokens = re.findall(r"[a-zA-Z_][\w'\-]*|\S", text.lower())
    return text.lower(), tokens


def _strip_leading(text: str) -> str:
    """Remove leading filler/politeness so imperatives surface."""
    for _ in range(8):
        stripped = text.lstrip(" \t,.!-–—:;~*")
        lowered = stripped.lower()
        matched_prefix = None
        for prefix in _POLITENESS_PREFIXES:
            if lowered.startswith(prefix + " ") or lowered == prefix:
                matched_prefix = prefix
                break
        if matched_prefix is None:
            first_word = lowered.split(" ", 1)[0] if lowered else ""
            if first_word in _FILLER_PREFIXES:
                matched_prefix = first_word
        if matched_prefix is None:
            return stripped
        remainder = stripped[len(matched_prefix):]
        if remainder.strip() == text.strip():
            break  # safety: never loop without progress
        text = remainder
    return text


def _find_verbs(tokens: list[str]) -> list[tuple[int, str]]:
    """Locate action verbs: [(index, family), …] earliest-first."""
    found: list[tuple[int, str]] = []
    joined = " ".join(tokens)
    # Multi-word forms live only in the joined text.
    for form in ("set up", "clean up"):
        idx = joined.find(form)
        if idx >= 0 and (idx == 0 or joined[idx - 1] == " "):
            word_index = len(joined[:idx].split()) if joined[:idx].strip() else 0
            prev_ok = word_index == 0 or tokens[word_index - 1] not in _SUBJECT_GUARD
            family = (
                "build" if form == "set up"
                else "modify"
            )
            if prev_ok:
                found.append((word_index, family))
    for i, token in enumerate(tokens):
        if token in _BUILD_VERBS:
            family = "build"
        elif token in _DEBUG_VERBS:
            family = "debug"
        elif token in _MODIFY_VERBS:
            family = "modify"
        else:
            continue
        prev_ok = i == 0 or tokens[i - 1] not in _SUBJECT_GUARD
        if prev_ok:
            found.append((i, family))
    seen: set[int] = set()
    unique: list[tuple[int, str]] = []
    for index, family in sorted(found, key=lambda item: item[0]):
        if index not in seen:
            unique.append((index, family))
            seen.add(index)
    return unique


def _family_category(family: str) -> str:
    return {
        "build": CODING_TASK,
        "debug": DEBUG_TASK,
        "modify": PROJECT_MODIFICATION,
    }[family]


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------

def classify_intent(message: str, env=None) -> IntentResult:
    """Classify *message* deterministically (no network, no provider)."""
    text, tokens = _normalize(message)
    core = _strip_leading(text)
    core_tokens = re.findall(r"[a-z_][\w'\-]*|\S", core)

    files = tuple(dict.fromkeys(
        m.group(0) for m in _FILE_EXT_RE.finditer(text)
    ))

    if not core or not core_tokens:
        return IntentResult(CONVERSATION, 0.95, ("empty",))

    first = core_tokens[0].rstrip(",.?!")
    if first in ("?", "!", ".", ";") or (
        len(core_tokens) <= 4
        and all(t.strip(",.?!") in _GREETING_TOKENS for t in core_tokens)
    ):
        return IntentResult(CONVERSATION, 0.95, ("greeting",), files)

    # Self-statements ("I enjoyed creating games", "we ship on Fridays")
    # describe the speaker — they are not directives to the assistant.
    if first in ("i", "i'm", "im", "we", "my", "our", "it", "they", "he",
                 "she", "you") and not _DEBUG_NOUNS.search(core):
        return IntentResult(CONVERSATION, 0.75, ("self_statement",), files)

    # Informational starters win over mid-sentence verbs, but explicit
    # suggestions ("how about adding dark mode?") fall through to the verb
    # scan with the suggestion prefix removed so the verb becomes leading.
    is_suggestion = core.startswith(("how about ", "what about "))
    if is_suggestion:
        core_tokens = core_tokens[2:]
        core = " ".join(t for t in core_tokens)

    info_hit = None
    if not is_suggestion:
        for starter in _INFO_STARTERS:
            if core.startswith(starter):
                info_hit = starter
                break
        if info_hit is None and text.endswith("?") and first in {
            "what", "whats", "who", "whose", "why", "when", "where", "which",
            "how", "is", "are", "am", "was", "were", "do", "does", "did",
            "should", "shall", "may", "might", "must", "can", "could",
            "would", "will",
        }:
            info_hit = "terminal-question"

    verbs = _find_verbs(core_tokens)

    if info_hit is not None and (not verbs or verbs[0][0] > 0):
        return IntentResult(QUESTION, 0.85, (info_hit,), files)

    # Large pastes that are not informational requests are build specs.
    if is_large_input(message, env=env):
        return IntentResult(CODING_TASK, 0.92, ("large_input",), files)

    artifact = bool(_ARTIFACT_RE.search(core)) or bool(_DEBUG_NOUNS.search(core))
    file_ref = bool(files) or bool(_THIS_CODEBASE_RE.search(core))
    pronoun = bool(_PRONOUN_OBJECT_RE.search(core))
    possessive = bool(_POSSESSIVE_RE.search(core))

    if verbs:
        first_index, first_family = verbs[0]
        confidence = 0.62 if first_index == 0 else 0.52
        bonus = 0.0
        if artifact:
            bonus += 0.20
        if file_ref:
            bonus += 0.22
        if pronoun and not artifact and not file_ref:
            bonus += 0.10
        if possessive:
            bonus += 0.06
        confidence = min(0.95, confidence + bonus)
        category = _family_category(first_family)
        evidence = [f"verb:{core_tokens[first_index]}" + f"@{first_index}"]
        if artifact:
            evidence.append("artifact")
        if file_ref:
            evidence.append("file")
        return IntentResult(category, confidence, tuple(evidence), files)

    if text.endswith("?") or info_hit is not None:
        return IntentResult(QUESTION, 0.80, ("question",), files)

    return IntentResult(CONVERSATION, 0.60, ("fallback",), files)


def is_coding_request(message: str, env=None) -> bool:
    """Convenience predicate (classification only — performs no routing)."""
    return classify_intent(message, env=env).is_coding_task
