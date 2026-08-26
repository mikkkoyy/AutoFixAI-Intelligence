"""AI chat-agent (GPT / Claude / DeepSeek) detection tests."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.chat_agents import CHAT_AGENT_ORDER, ChatAgentInfo, detect_chat_agents


class TestChatAgentDetection:
    def test_all_three_agents_reported(self):
        result = detect_chat_agents(env={})
        assert set(result) == {"GPT", "Claude", "DeepSeek"}
        assert CHAT_AGENT_ORDER == ("GPT", "Claude", "DeepSeek")

    def test_no_keys_means_all_unavailable(self):
        result = detect_chat_agents(env={})
        for info in result.values():
            assert info.available is False
            assert info.detail

    def test_openai_key_marks_gpt_available(self):
        result = detect_chat_agents(env={"OPENAI_API_KEY": "sk-test"})
        assert result["GPT"].available is True
        assert result["Claude"].available is False
        assert result["DeepSeek"].available is False

    def test_anthropic_key_marks_claude_available(self):
        result = detect_chat_agents(env={"ANTHROPIC_API_KEY": "sk-ant"})
        assert result["Claude"].available is True
        assert result["GPT"].available is False

    def test_deepseek_key_marks_deepseek_available(self):
        result = detect_chat_agents(env={"DEEPSEEK_API_KEY": "sk-ds"})
        assert result["DeepSeek"].available is True

    def test_blank_key_does_not_count(self):
        result = detect_chat_agents(env={"OPENAI_API_KEY": "   "})
        assert result["GPT"].available is False

    def test_autofix_provider_pair_maps_to_agent(self):
        result = detect_chat_agents(
            env={"AUTOFIX_PROVIDER": "anthropic", "AUTOFIX_API_KEY": "k"}
        )
        assert result["Claude"].available is True
        assert "AUTOFIX_PROVIDER" in result["Claude"].detail

    def test_autofix_provider_without_key_unavailable(self):
        result = detect_chat_agents(env={"AUTOFIX_PROVIDER": "openai"})
        assert result["GPT"].available is False

    def test_real_environment_detection_never_raises(self):
        result = detect_chat_agents()
        for name in CHAT_AGENT_ORDER:
            assert isinstance(result[name], ChatAgentInfo)
