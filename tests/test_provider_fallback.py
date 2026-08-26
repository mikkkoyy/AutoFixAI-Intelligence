"""Automatic AI provider connection, fallback and local-only behavior.

Covers spec Part 2:
- providers are auto-detected from existing configuration (25)
- the preferred/configured provider is selected automatically
- on failure the next configured provider is tried (26)
- LocalAssistant is used ONLY when no usable provider exists (27)
- authentication errors are handled cleanly without leaking credentials
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.agents import chat_provider as cp


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
        "AUTOFIX_PROVIDER", "AUTOFIX_API_KEY", "AUTOFIX_BASE_URL",
        "AUTOFIX_CHAT_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)


# ── Auto-detection / ordering ─────────────────────────────────────


class TestProviderChain:
    def test_no_keys_means_no_providers(self):
        assert cp.provider_chain(env={}) == []
        assert cp.available_providers(env={}) == []

    def test_each_key_detected(self):
        assert [p.name for p in cp.provider_chain(
            env={"OPENAI_API_KEY": "k"} )] == ["GPT"]
        assert [p.name for p in cp.provider_chain(
            env={"ANTHROPIC_API_KEY": "k"})] == ["Claude"]
        assert [p.name for p in cp.provider_chain(
            env={"DEEPSEEK_API_KEY": "k"})] == ["DeepSeek"]

    def test_multiple_configured_all_included(self):
        chain = cp.provider_chain(env={
            "OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b",
            "DEEPSEEK_API_KEY": "c",
        })
        assert len(chain) == 3
        assert {p.name for p in chain} == {"GPT", "Claude", "DeepSeek"}

    def test_autofix_provider_is_preferred(self):
        chain = cp.provider_chain(env={
            "OPENAI_API_KEY": "a",
            "AUTOFIX_PROVIDER": "openai-compatible",
            "AUTOFIX_API_KEY": "k",
            "AUTOFIX_BASE_URL": "https://llm.example.internal/v1",
        })
        assert chain[0].name == "AutoFix Model"
        assert chain[1].name == "GPT"

    def test_explicit_chat_provider_override_wins(self):
        chain = cp.provider_chain(env={
            "OPENAI_API_KEY": "a",
            "ANTHROPIC_API_KEY": "b",
            "AUTOFIX_CHAT_PROVIDER": "claude",
        })
        assert chain[0].name == "Claude"

    def test_no_manual_selection_required(self):
        """Detection uses ONLY environment configuration — no UI state."""
        import inspect

        source = inspect.getsource(cp.provider_chain)
        assert "QComboBox" not in source and "set_ai_mode" not in source


# ── Fallback between providers ────────────────────────────────────


class TestProviderFallback:
    def test_first_usable_provider_answers(self, monkeypatch):
        calls = []

        def fake_call(config, message, history=None):
            calls.append(config.name)
            return f"answer from {config.name}"

        monkeypatch.setattr(cp, "call_provider", fake_call)
        reply = cp.converse("hi", ".", env={
            "OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b",
        })
        assert reply == "answer from GPT"
        assert calls == ["GPT"]  # second never contacted

    def test_failure_falls_through_to_next_provider(self, monkeypatch):
        order = []

        def fake_call(config, message, history=None):
            order.append(config.name)
            if config.name == "GPT":
                raise cp.ChatProviderError("GPT HTTP 401: bad key")
            return "recovered answer"

        monkeypatch.setattr(cp, "call_provider", fake_call)
        reply = cp.converse("hi", ".", env={
            "OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b",
        })
        assert reply == "recovered answer"
        assert order == ["GPT", "Claude"]

    def test_auth_error_then_third_provider(self, monkeypatch):
        def fake_call(config, message, history=None):
            if config.name != "DeepSeek":
                raise cp.ChatProviderError(f"{config.name} HTTP 401")
            return "deepseek ok"

        monkeypatch.setattr(cp, "call_provider", fake_call)
        reply = cp.converse("hi", ".", env={
            "OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b",
            "DEEPSEEK_API_KEY": "c",
        })
        assert reply == "deepseek ok"

    def test_all_fail_raises_clean_error_without_secrets(self, monkeypatch):
        def fake_call(config, message, history=None):
            raise cp.ChatProviderError(
                f"{config.name} HTTP 401: invalid key sk-supersecret123456"
            )

        monkeypatch.setattr(cp, "call_provider", fake_call)
        with pytest.raises(cp.ChatProviderError) as excinfo:
            cp.converse("hi", ".", env={
                "OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b",
            })
        blob = str(excinfo.value)
        assert "sk-supersecret" not in blob      # credential never leaks
        assert "GPT" in blob and "Claude" in blob  # honest about what failed

    def test_analyze_uses_fallback_too(self, monkeypatch):
        def fake_call(config, prompt):
            if config.name == "GPT":
                raise cp.ChatProviderError("GPT connection failed")
            return "plan text"

        monkeypatch.setattr(cp, "call_provider", fake_call)
        plan, source = cp.analyze("build a widget", ".", env={
            "OPENAI_API_KEY": "a", "DEEPSEEK_API_KEY": "d",
        })
        assert plan == "plan text" and source == "DeepSeek"


# ── Local assistant only when nothing usable exists ───────────────


class TestLocalOnlyWhenUnconfigured:
    def test_local_assistant_when_no_provider_configured(self, tmp_path):
        reply = cp.converse("hello", tmp_path, env={})
        assert "local mode" in reply.lower()

    def test_local_assistant_not_used_when_provider_fails(
        self, monkeypatch, tmp_path
    ):
        def fake_call(config, message, history=None):
            raise cp.ChatProviderError(f"{config.name} down")

        monkeypatch.setattr(cp, "call_provider", fake_call)
        with pytest.raises(cp.ChatProviderError):
            cp.converse("hello", tmp_path, env={"OPENAI_API_KEY": "k"})

    def test_error_detail_is_redacted_for_display(self):
        cleaned = cp._clean_provider_error(
            "GPT HTTP 401: key sk-abcdefghijklmnop was rejected"
        )
        assert "sk-abcdefghijklmnop" not in cleaned
