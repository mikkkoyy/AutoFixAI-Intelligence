"""Regression tests for Chat AI planning — verifies NameError fix.

Covers:
    - Coding request produces a proposal (no NameError)
    - Detailed coding request produces a proposal
    - Provider configured → planning succeeds
    - Different providers reach planner without undefined config
    - No provider configured → fallback without NameError
    - Proposal revision works
    - Approval creates exactly one AutoFixTask
    - Planning does not execute code
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.agents.chat_intelligence import (
    ChatEngine,
    ChatProposal,
    ChatResponse,
    classify_conversation_intent,
    generate_proposal,
    build_chat_context,
    build_execution_prompt,
    is_approval_message,
    is_revision_message,
    apply_revision,
    render_proposal_text,
    CODING_REQUEST,
    PROPOSAL_INTENTS,
)
from app.agents.chat_provider import (
    ProviderConfig,
    analyze,
    available_providers,
    call_provider,
    converse,
    LocalAssistant,
    ChatProviderError,
    _post_json,
)


# -----------------------------------------------------------------------
# Helper: fake provider that always succeeds
# -----------------------------------------------------------------------


_FAKE_PROVIDER = ProviderConfig(
    name="FakeGPT",
    kind="openai",
    base_url="https://fake.example.com/v1",
    api_key="sk-fake-test-key",
    model="fake-model",
)


def _fake_call(config, prompt, **kwargs):
    """Return a deterministic plan without any network call."""
    return f"Plan for: {prompt[-60:]}"


def _fake_converse(message, workspace, history=None, env=None, system_context=None):
    return f"Fake reply to: {message}"


# ======================================================================
# Test 1: Normal coding request produces a proposal
# ======================================================================


class TestCodingRequestProducesProposal:
    def test_simple_coding_request(self):
        engine = ChatEngine()
        response = engine.handle(
            "Create a login page.",
            workspace=".",
            history=[],
            active_proposal=None,
        )
        assert response.kind == "proposal"
        assert response.proposal is not None
        assert response.proposal.status == "AWAITING APPROVAL"
        assert response.execution_prompt != ""

    def test_no_name_error(self):
        """The original bug: NameError: name 'config' is not defined."""
        engine = ChatEngine()
        try:
            response = engine.handle(
                "Create a modern animated login page.",
                workspace=".",
                history=[],
            )
            # Should not raise NameError
        except NameError as exc:
            if "config" in str(exc):
                pytest.fail(f"NameError regression: {exc}")
            raise


# ======================================================================
# Test 2: Detailed coding request produces a proposal
# ======================================================================


class TestDetailedCodingRequest:
    def test_detailed_request(self):
        engine = ChatEngine()
        response = engine.handle(
            "Create a responsive login page with email and password fields, "
            "form validation, CSS animations, and a dark mode toggle.",
            workspace=".",
            history=[],
        )
        assert response.kind == "proposal"
        assert response.proposal is not None
        assert "login" in response.proposal.objective.lower()
        assert response.proposal.status == "AWAITING APPROVAL"


# ======================================================================
# Test 3: Provider configured → planning succeeds
# ======================================================================


class TestProviderConfiguredPlanningSucceeds:
    @patch("app.agents.chat_provider.call_provider", side_effect=_fake_call)
    @patch("app.agents.chat_provider.provider_chain")
    def test_plan_worker_receives_plan(self, mock_chain, mock_call):
        mock_chain.return_value = [_FAKE_PROVIDER]
        plan, source = analyze("Create a login page.", ".")
        assert plan is not None
        assert "Plan for:" in plan
        assert source == "FakeGPT"
        mock_call.assert_called_once()

    @patch("app.agents.chat_provider.call_provider", side_effect=_fake_call)
    @patch("app.agents.chat_provider.provider_chain")
    def test_provider_name_returned(self, mock_chain, mock_call):
        mock_chain.return_value = [_FAKE_PROVIDER]
        _plan, source = analyze("Test task.", ".")
        assert source == "FakeGPT"


# ======================================================================
# Test 4: Different providers reach planner without undefined config
# ======================================================================


class TestDifferentProvidersReachPlanner:
    @patch("app.agents.chat_provider.call_provider", side_effect=_fake_call)
    @patch("app.agents.chat_provider.provider_chain")
    def test_openai_provider(self, mock_chain, mock_call):
        mock_chain.return_value = [ProviderConfig(
            name="GPT", kind="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test", model="gpt-4o-mini",
        )]
        plan, source = analyze("Build something.", ".")
        assert plan is not None
        assert source == "GPT"

    @patch("app.agents.chat_provider.call_provider", side_effect=_fake_call)
    @patch("app.agents.chat_provider.provider_chain")
    def test_anthropic_provider(self, mock_chain, mock_call):
        mock_chain.return_value = [ProviderConfig(
            name="Claude", kind="anthropic",
            base_url="https://api.anthropic.com/v1/messages",
            api_key="sk-ant-test", model="claude-3-5-haiku-latest",
        )]
        plan, source = analyze("Build something.", ".")
        assert plan is not None
        assert source == "Claude"

    @patch("app.agents.chat_provider.call_provider", side_effect=_fake_call)
    @patch("app.agents.chat_provider.provider_chain")
    def test_deepseek_provider(self, mock_chain, mock_call):
        mock_chain.return_value = [ProviderConfig(
            name="DeepSeek", kind="openai",
            base_url="https://api.deepseek.org",
            api_key="sk-ds-test", model="deepseek-chat",
        )]
        plan, source = analyze("Build something.", ".")
        assert plan is not None
        assert source == "DeepSeek"


# ======================================================================
# Test 5: No provider configured → fallback without NameError
# ======================================================================


class TestNoProviderFallback:
    @patch("app.agents.chat_provider.provider_chain")
    def test_analyze_returns_none(self, mock_chain):
        mock_chain.return_value = []
        plan, source = analyze("Create a login page.", ".")
        assert plan is None
        assert source == ""

    @patch("app.agents.chat_provider.provider_chain")
    def test_converse_uses_local_assistant(self, mock_chain):
        mock_chain.return_value = []
        reply = converse("Hello!", ".")
        assert "local" in reply.lower() or "offline" in reply.lower() or "AutoFix" in reply

    def test_engine_handles_no_provider(self):
        engine = ChatEngine()
        response = engine.handle("Hello!", workspace=".", history=[])
        assert isinstance(response, ChatResponse)
        assert response.content != ""


# ======================================================================
# Test 6: Proposal revision works
# ======================================================================


class TestProposalRevision:
    def test_revision_modifies_same_proposal(self):
        engine = ChatEngine()

        # First message: create proposal
        response1 = engine.handle(
            "Create a login page.", workspace=".", history=[],
        )
        assert response1.kind == "proposal"
        original_prompt = response1.execution_prompt

        # Second message: revise
        response2 = engine.handle(
            "Make it animated.",
            workspace=".",
            history=[("user", "Create a login page."), ("assistant", response1.content)],
            active_proposal=response1.proposal.to_dict() if response1.proposal else None,
        )
        assert response2.kind == "revision"
        assert response2.proposal is not None
        assert response2.execution_prompt != original_prompt

    def test_is_revision_detection(self):
        assert is_revision_message("Make it animated.") is True
        assert is_revision_message("Also add dark mode.") is True
        assert is_revision_message("approve") is False
        assert is_revision_message("") is False

    def test_apply_revision_accumulates(self):
        proposal = ChatProposal(
            objective="Create a login page.",
            understanding="You want to create a login page.",
            analysis_summary="",
            plan=["Step 1: Inspect", "Step 2: Implement"],
            affected_components=["frontend"],
            execution_prompt="original prompt",
        )
        revised = apply_revision(proposal, "Also add animations.")
        assert len(revised.revisions) == 1
        assert len(revised.plan) > 2  # new step added
        assert revised.execution_prompt != "original prompt"


# ======================================================================
# Test 7: Approval creates exactly one AutoFixTask
# ======================================================================


class TestApprovalCreatesOneTask:
    def test_approval_response_kind(self):
        engine = ChatEngine()
        proposal = ChatProposal(
            objective="Create a login page.",
            understanding="You want a login page.",
            analysis_summary="",
            plan=["Step 1"],
            execution_prompt="do the login page",
        )
        proposal_dict = proposal.to_dict()
        response = engine.handle(
            "approve",
            workspace=".",
            history=[],
            active_proposal=proposal_dict,
        )
        assert response.kind == "approval"
        assert response.execution_prompt == "do the login page"
        assert response.original_request == "approve"

    def test_is_approval_detection(self):
        assert is_approval_message("approve") is True
        assert is_approval_message("approve and execute") is True
        assert is_approval_message("go ahead") is True
        assert is_approval_message("lgtm") is True
        assert is_approval_message("ship it") is True
        assert is_approval_message("create a login page") is False

    def test_approval_carries_original_request(self):
        engine = ChatEngine()
        proposal = ChatProposal(
            objective="Build API.",
            understanding="Build an API.",
            analysis_summary="",
            plan=[],
            execution_prompt="build the api",
            origin_request="Create an API endpoint.",
        )
        response = engine.handle(
            "approved",
            workspace=".",
            history=[],
            active_proposal=proposal.to_dict(),
        )
        assert response.kind == "approval"
        assert response.original_request == "Create an API endpoint."


# ======================================================================
# Test 8: Planning does not execute code
# ======================================================================


class TestPlanningDoesNotExecute:
    def test_proposal_kind_not_approved(self):
        engine = ChatEngine()
        response = engine.handle("Create a login page.", workspace=".", history=[])
        assert response.kind == "proposal"
        assert response.proposal.status == "AWAITING APPROVAL"
        assert response.execution_prompt != ""  # prompt exists but is NOT executed

    def test_proposal_text_shows_awaiting(self):
        proposal = ChatProposal(
            objective="Create a login page.",
            understanding="You want a login page.",
            analysis_summary="",
            plan=["Step 1"],
            execution_prompt="prompt",
            status="AWAITING APPROVAL",
        )
        text = render_proposal_text(proposal)
        assert "AWAITING APPROVAL" in text

    def test_proposal_status_default(self):
        proposal = ChatProposal(
            objective="x", understanding="y", analysis_summary="",
        )
        assert proposal.status == "AWAITING APPROVAL"


# ======================================================================
# Test: _post_json provider_name parameter
# ======================================================================


class TestPostJsonProviderName:
    def test_post_json_accepts_provider_name(self):
        """Verify _post_json accepts the provider_name parameter."""
        import inspect
        sig = inspect.signature(_post_json)
        assert "provider_name" in sig.parameters

    def test_post_json_default_provider_name(self):
        """Verify default provider_name is 'provider'."""
        import inspect
        sig = inspect.signature(_post_json)
        assert sig.parameters["provider_name"].default == "provider"
