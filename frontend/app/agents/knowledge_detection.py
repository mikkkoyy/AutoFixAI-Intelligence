"""Reusable-knowledge detection for AI Chat.

Deterministic, offline heuristics that recognise when a conversation
exchange produced POTENTIALLY reusable knowledge:

- a better planning technique
- a reusable debugging strategy / root-cause insight
- a useful reasoning or decision pattern
- a better assistant behavior rule
- an explicit "remember/share this" request

Detection NEVER saves anything and never touches the network — it only
produces a :class:`KnowledgeProposal` for the UI approval workflow.  The
distilled body is secret-redacted at detection time (defense in depth; the
save path re-vets everything).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.github_knowledge import KNOWLEDGE_CATEGORIES
from app.agents.task_memory import redact_secrets


@dataclass
class KnowledgeProposal:
    """A knowledge candidate awaiting the user's Review/Save/Ignore choice."""

    title: str
    category: str          # one of KNOWLEDGE_CATEGORIES
    body: str              # distilled markdown (already redacted)
    source: str            # safe context description (no raw transcripts)
    confidence: float      # 0..1 heuristic confidence

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "category": self.category,
            "body": self.body,
            "source": self.source,
            "confidence": round(float(self.confidence), 2),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeProposal":
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**payload)


# Strong lesson/insight markers (assistant text).
_INSIGHT_MARKERS = (
    r"\broot cause\b",
    r"\bturns? out\b",
    r"\blesson learned\b",
    r"\blearned that\b",
    r"\bkey insight\b",
    r"\bgotcha\b",
    r"\bbetter approach\b",
    r"\bproven (pattern|approach|strategy)\b",
    r"\bwhat (actually )?worked\b",
    r"\bthe fix (is|was)\b",
    r"\breusable (pattern|strategy|technique)\b",
    r"\bbest practice\b",
)

# Explicit share requests (user text) — highest-signal trigger.
_SHARE_REQUEST_MARKERS = (
    r"\b(remember|save|store|share|keep)\s+(this|that|it)\b",
    r"\badd (this |that )?to (the )?(knowledge|shared knowledge|knowledge base)\b",
    r"\bworth (sharing|remembering|saving)\b",
)

# Category routing.
_CATEGORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("planning", ("plan", "planning", "strategy", "architecture",
                  "decompose", "break down", "roadmap", "milestone")),
    ("reasoning", ("debug", "root cause", "diagnos", "why ", "trace",
                   "investigate", "hypothesis", "decision")),
    ("behavior", ("response", "behavior", "tone", "assistant", "answer style",
                  "clarify", "ask first")),
    ("patterns", ("pattern", "convention", "idiom", "template",
                  "abstraction", "interface")),
    ("lessons", ("lesson", "learned", "gotcha", "pitfall", "mistake",
                 "turns out", "regression")),
)


def _count_markers(text: str, patterns) -> int:
    return sum(1 for pattern in patterns if re.search(pattern, text, re.IGNORECASE))


def _guess_category(text: str) -> str:
    lowered = text.lower()
    best_category, best_hits = "lessons", 0
    for category, hints in _CATEGORY_HINTS:
        hits = sum(1 for hint in hints if hint in lowered)
        if hits > best_hits:
            best_category, best_hits = category, hits
    return best_category if best_hits else "lessons"


_TITLE_STOP = re.compile(
    r"^(the|a|an|we|i|it|so|and|that|this|turns out|okay|ok)\b[ :,-]*", re.IGNORECASE
)


def _make_title(insight_sentence: str) -> str:
    sentence = re.sub(r"\s+", " ", insight_sentence).strip()
    sentence = _TITLE_STOP.sub("", sentence)
    words = sentence.split()
    if len(words) > 10:
        words = words[:10]
    title = " ".join(words).rstrip(".,;:!?")
    return (title[:80] or "Reusable insight") if title else "Reusable insight"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def detect_reusable_knowledge(
    user_message: str,
    assistant_reply: str,
    source_context: str = "",
) -> KnowledgeProposal | None:
    """Return a proposal when the exchange looks genuinely reusable.

    Requires EITHER an explicit share request from the user OR at least two
    independent insight markers — single weak markers never trigger, so the
    notification stays rare and meaningful.
    """
    user = user_message or ""
    reply = assistant_reply or ""
    combined = f"{user}\n{reply}"

    share_hits = _count_markers(user, _SHARE_REQUEST_MARKERS)
    insight_hits = _count_markers(combined, _INSIGHT_MARKERS)

    if share_hits == 0 and insight_hits < 2:
        return None

    # Distil body from sentences carrying markers (never dump raw transcript).
    marker_res = [re.compile(p, re.IGNORECASE) for p in _INSIGHT_MARKERS]
    chosen: list[str] = []
    for sentence in _sentences(reply) + _sentences(user):
        if any(r.search(sentence) for r in marker_res):
            if sentence not in chosen:
                chosen.append(sentence)
        if len(chosen) >= 4:
            break
    if not chosen:
        chosen = [_first_meaningful(reply)] if _first_meaningful(reply) else []
    if not chosen:
        return None

    bullets = "\n".join(f"- {redact_secrets(s)}" for s in chosen)
    title_source = chosen[0]
    title = redact_secrets(_make_title(title_source))

    confidence = 0.55 + 0.08 * min(insight_hits, 4) + (0.15 if share_hits else 0.0)
    confidence = min(confidence, 0.95)

    category = _guess_category(combined)
    source = (source_context or "").strip() or "Chat conversation"

    return KnowledgeProposal(
        title=title,
        category=category if category in KNOWLEDGE_CATEGORIES else "lessons",
        body=bullets,
        source=redact_secrets(source),
        confidence=confidence,
    )


def _first_meaningful(text: str) -> str:
    for sentence in _sentences(text):
        if len(sentence.split()) >= 6:
            return sentence
    return ""


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())
