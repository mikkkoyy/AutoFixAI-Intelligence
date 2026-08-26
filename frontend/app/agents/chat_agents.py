"""AI chat-agent (reasoning provider) detection.

Chat agents plan and reason — they never execute code.  Availability is
detected from configured provider credentials only; nothing is installed,
probed over the network, or fabricated:

    GPT      — OPENAI_API_KEY
    Claude   — ANTHROPIC_API_KEY
    DeepSeek — DEEPSEEK_API_KEY

The backend's AUTOFIX_PROVIDER / AUTOFIX_API_KEY pair (see backend/.env)
is honoured as a secondary signal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Display order in the right-side panel.
CHAT_AGENT_ORDER = ("GPT", "Claude", "DeepSeek")

_PROVIDER_ENV_KEYS = {
    "GPT": ("OPENAI_API_KEY",),
    "Claude": ("ANTHROPIC_API_KEY",),
    "DeepSeek": ("DEEPSEEK_API_KEY",),
}

_PROVIDER_ALIASES = {
    "gpt": "GPT",
    "openai": "GPT",
    "claude": "Claude",
    "anthropic": "Claude",
    "deepseek": "DeepSeek",
}


@dataclass
class ChatAgentInfo:
    """Detection result for a single chat/reasoning agent."""

    name: str
    available: bool
    detail: str = ""


def detect_chat_agents(env=None) -> dict[str, ChatAgentInfo]:
    """Return availability for each chat agent based on *env* (default os.environ)."""
    env = os.environ if env is None else env

    provider = str(env.get("AUTOFIX_PROVIDER", "")).strip().lower()
    shared_key = str(env.get("AUTOFIX_API_KEY", "")).strip()
    provider_agent = _PROVIDER_ALIASES.get(provider)

    result: dict[str, ChatAgentInfo] = {}
    for name in CHAT_AGENT_ORDER:
        key = next(
            (
                str(env.get(k, "")).strip()
                for k in _PROVIDER_ENV_KEYS[name]
                if str(env.get(k, "")).strip()
            ),
            "",
        )
        if key:
            result[name] = ChatAgentInfo(name, True, "API key configured")
        elif provider_agent == name and shared_key:
            result[name] = ChatAgentInfo(
                name, True, f"via AUTOFIX_PROVIDER={provider}"
            )
        else:
            hint = "/".join(_PROVIDER_ENV_KEYS[name])
            result[name] = ChatAgentInfo(name, False, f"{hint} not set")
    return result
