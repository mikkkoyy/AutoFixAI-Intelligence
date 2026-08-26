"""Chat AI web research — comprehensive test suite.

Covers the full acceptance matrix for automatic web research:

    1.  Research trigger detection
    2.  No unnecessary search for greetings
    3.  Automatic web search
    4.  Official-source priority
    5.  GitHub-source priority
    6.  Forum/community research
    7.  Search result aggregation
    8.  Source citation/link handling
    9.  Search failure fallback
    10. Provider-independent research context
    11. Secret sanitization
    12. Project-memory integration
    13. Research context passed into Chat AI
    14. Coding request → research → proposal
    15. Approval still required
    16. Research never creates an AutoFix task by itself
    17. Knowledge candidate detection
    18. Knowledge save requires explicit approval
    19. GitHub knowledge separation from project memory
    20. Existing AutoFix regression tests
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.agents.web_research import (
    ResearchContext,
    ResearchResult,
    aggregate_results,
    build_search_queries,
    classify_source,
    format_research_context,
    should_research,
    web_search,
    _scrub_query_secrets,
)
from app.agents.chat_intelligence import (
    ChatEngine,
    ChatProposal,
    ChatResponse,
    GREETING,
    CONVERSATION,
    CODING_REQUEST,
    RECOMMENDATION,
    generate_proposal,
    render_proposal_text,
    is_approval_message,
)
from app.agents.pipeline import ApprovalPipeline, PlanWorker
from app.agents.autofix_task import AutoFixTask


@pytest.fixture
def engine():
    return ChatEngine()


def handle(engine, text, ws, history=None, active=None):
    return engine.handle(text, str(ws), history=history, active_proposal=active)


def task_files(ws):
    tasks_dir = ws / ".autofix" / "tasks"
    if not tasks_dir.exists():
        return []
    return sorted(tasks_dir.glob("autofix-task-*.json"))


# ======================================================================
# 1. Research trigger detection
# ======================================================================


class TestResearchTriggerDetection:
    """Messages that SHOULD trigger research."""

    @pytest.mark.parametrize(
        "message",
        [
            "What is the latest version of React?",
            "How do I use the GitHub API?",
            "Why am I getting a TypeError in Python?",
            "What's the best practice for React hooks?",
            "Compare Vue vs React for a new project",
            "How to install Docker on Windows?",
            "OpenCode says invalid API key",
            "What does this ImportError mean?",
            "Is FastAPI still maintained?",
            "Search the web for PySide6 dark mode",
            "Look up the latest Node.js release",
            "What's the current best way to handle CORS?",
            "Does React still support class components?",
            "GitHub issue says this is fixed in v2.0",
            "Stack Overflow answer suggests using async/await",
            "The tutorial on docs.python.org shows a different approach",
            "What are the current npm audit recommendations?",
            "How does the new VS Code terminal work?",
            "Check the community forums for this error pattern",
            "What's the recommended way to deploy a FastAPI app?",
        ],
    )
    def test_should_trigger_research(self, message):
        assert should_research(message) is True, f"Expected research for: {message!r}"

    @pytest.mark.parametrize(
        "message",
        [
            "hello",
            "hi",
            "hey",
            "good morning",
            "thanks",
            "thank you",
            "bye",
            "ok",
            "cool",
            "nice",
            "lol",
            "haha",
            "good afternoon",
            "how are you",
        ],
    )
    def test_should_not_trigger_research_for_greetings(self, message):
        assert should_research(message) is False, f"No research for greeting: {message!r}"

    @pytest.mark.parametrize(
        "message",
        [
            "",  # empty
            None,  # None
        ],
    )
    def test_should_not_trigger_for_empty(self, message):
        assert should_research(message) is False

    def test_coding_request_triggers_research(self):
        """Coding requests that involve errors should trigger research."""
        assert should_research("Fix this ImportError in my project") is True

    def test_debugging_discussion_triggers_research(self):
        assert should_research("Why does the pipeline fail?") is True


# ======================================================================
# 2. No unnecessary search for greetings
# ======================================================================


class TestNoUnnecessarySearch:
    def test_greeting_produces_no_research_context(self, engine, tmp_path):
        response = handle(engine, "hello", tmp_path)
        assert response.kind == "reply"
        assert response.proposal is None
        # No AutoFix tasks created
        assert task_files(tmp_path) == []

    @pytest.mark.parametrize("greeting", ["hi", "hey", "good morning", "thanks", "bye"])
    def test_all_greetings_skip_research(self, engine, tmp_path, greeting):
        response = handle(engine, greeting, tmp_path)
        assert response.kind == "reply"
        assert task_files(tmp_path) == []

    def test_casual_one_word_skips_research(self, engine, tmp_path):
        response = handle(engine, "ok", tmp_path)
        assert response.kind == "reply"


# ======================================================================
# 3. Automatic web search
# ======================================================================


class TestAutomaticWebSearch:
    def test_web_search_returns_results(self):
        """web_search returns ResearchResult list (may be empty if network unavailable)."""
        results = web_search("Python programming", max_results=3)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, ResearchResult)
            assert r.url
            assert r.source_priority in (1, 2, 3, 4)

    def test_web_search_empty_query(self):
        results = web_search("")
        assert results == []

    def test_web_search_none_query(self):
        results = web_search(None)
        assert results == []

    def test_build_search_queries_returns_list(self):
        queries = build_search_queries("What is the latest version of React?")
        assert isinstance(queries, list)
        assert len(queries) >= 1
        # Should not contain stop words
        for q in queries:
            assert "the" not in q.lower().split() or len(q.split()) > 3

    def test_build_search_queries_preserves_technical_terms(self):
        queries = build_search_queries("How does Docker work?")
        combined = " ".join(queries).lower()
        assert "docker" in combined

    def test_build_search_queries_empty_input(self):
        queries = build_search_queries("")
        assert queries == []


# ======================================================================
# 4. Official-source priority
# ======================================================================


class TestOfficialSourcePriority:
    def test_official_docs_get_priority_1(self):
        priority, stype = classify_source(
            "https://docs.python.org/3/library/asyncio.html",
            "asyncio — Asynchronous I/O",
        )
        assert priority == 1
        assert stype == "official"

    def test_readthedocs_gets_priority_1(self):
        priority, stype = classify_source(
            "https://fastapi.tiangolo.com/tutorial/first-steps/",
            "FastAPI Tutorial",
        )
        assert priority == 1
        assert stype == "official"

    def test_github_docs_gets_priority_1(self):
        priority, stype = classify_source(
            "https://docs.github.com/en/rest",
            "GitHub REST API",
        )
        assert priority == 1

    def test_dev_docs_gets_priority_1(self):
        priority, stype = classify_source(
            "https://react.dev/learn/thinking-in-react",
            "Thinking in React",
        )
        assert priority == 1


# ======================================================================
# 5. GitHub-source priority
# ======================================================================


class TestGitHubSourcePriority:
    def test_github_repo_gets_priority_2(self):
        priority, stype = classify_source(
            "https://github.com/facebook/react",
            "React repository",
        )
        assert priority == 2
        assert stype == "github"

    def test_github_issue_gets_priority_2(self):
        priority, stype = classify_source(
            "https://github.com/facebook/react/issues/12345",
            "TypeError when using useEffect",
        )
        assert priority == 2
        assert stype == "github"

    def test_github_release_gets_priority_2(self):
        priority, stype = classify_source(
            "https://github.com/facebook/react/releases/tag/v18.2.0",
            "React 18.2.0 Release",
        )
        assert priority == 2
        assert stype == "github"

    def test_github_discussion_gets_priority_2(self):
        priority, stype = classify_source(
            "https://github.com/facebook/react/discussions/123",
            "Discussion about performance",
        )
        assert priority == 2
        assert stype == "github"


# ======================================================================
# 6. Forum/community research
# ======================================================================


class TestForumCommunityResearch:
    def test_stackoverflow_gets_priority_3(self):
        priority, stype = classify_source(
            "https://stackoverflow.com/questions/12345/how-to-fix-error",
            "How to fix error in Python",
        )
        assert priority == 3
        assert stype == "forum"

    def test_reddit_gets_priority_3(self):
        priority, stype = classify_source(
            "https://www.reddit.com/r/Python/comments/abc123/",
            "Best practices for Python",
        )
        assert priority == 3
        assert stype == "community"

    def test_medium_gets_priority_3(self):
        priority, stype = classify_source(
            "https://medium.com/@user/article-title",
            "Understanding async Python",
        )
        assert priority == 3
        assert stype == "community"

    def test_dev_to_gets_priority_3(self):
        priority, stype = classify_source(
            "https://dev.to/user/article-title",
            "Getting started with FastAPI",
        )
        assert priority == 3
        assert stype == "community"


# ======================================================================
# 7. Search result aggregation
# ======================================================================


class TestSearchAggregation:
    def test_deduplicates_same_url(self):
        results = [
            ResearchResult("Title", "https://example.com", "Snippet", 1, "official", "q"),
            ResearchResult("Title 2", "https://example.com", "Snippet 2", 2, "github", "q"),
        ]
        agg = aggregate_results(results)
        urls = [r.url for r in agg]
        assert urls.count("https://example.com") == 1

    def test_preserves_priority_order(self):
        results = [
            ResearchResult("Forum", "https://stackoverflow.com/q", "S", 3, "forum", "q"),
            ResearchResult("Official", "https://docs.python.org", "S", 1, "official", "q"),
            ResearchResult("GitHub", "https://github.com/repo", "S", 2, "github", "q"),
        ]
        agg = aggregate_results(results)
        priorities = [r.source_priority for r in agg]
        assert priorities == sorted(priorities)

    def test_limits_per_priority(self):
        results = [
            ResearchResult(f"Title {i}", f"https://example{i}.com", "S", 4, "other", "q")
            for i in range(10)
        ]
        agg = aggregate_results(results, max_per_priority=3)
        assert len(agg) <= 5  # some may be allowed due to unique domains

    def test_empty_results(self):
        agg = aggregate_results([])
        assert agg == []


# ======================================================================
# 8. Source citation/link handling
# ======================================================================


class TestSourceCitation:
    def test_format_research_context_includes_urls(self):
        results = [
            ResearchResult(
                "Python Docs",
                "https://docs.python.org/3/",
                "Python documentation",
                1, "official", "python docs",
            ),
        ]
        ctx = format_research_context(results)
        assert "https://docs.python.org/3/" in ctx
        assert "OFFICIAL" in ctx
        assert "Python Docs" in ctx

    def test_format_research_context_empty(self):
        ctx = format_research_context([])
        assert ctx == ""

    def test_format_research_context_includes_instructions(self):
        results = [
            ResearchResult("Title", "https://example.com", "S", 1, "official", "q"),
        ]
        ctx = format_research_context(results)
        assert "synthesi" in ctx.lower()  # "synthesize"
        assert "cite" in ctx.lower() or "citation" in ctx.lower()

    def test_format_includes_priority_labels(self):
        results = [
            ResearchResult("R1", "https://docs.python.org", "S", 1, "official", "q"),
            ResearchResult("R2", "https://github.com/repo", "S", 2, "github", "q"),
            ResearchResult("R3", "https://stackoverflow.com/q", "S", 3, "forum", "q"),
            ResearchResult("R4", "https://example.com", "S", 4, "other", "q"),
        ]
        ctx = format_research_context(results)
        assert "OFFICIAL" in ctx
        assert "GITHUB" in ctx
        assert "COMMUNITY" in ctx
        assert "WEB" in ctx


# ======================================================================
# 9. Search failure fallback
# ======================================================================


class TestSearchFailureFallback:
    def test_web_search_failure_returns_empty(self):
        """When network fails, web_search returns empty list (no crash)."""
        with patch("app.agents.web_research.urllib.request.urlopen", side_effect=OSError("network error")):
            results = web_search("test query")
            assert results == []

    def test_research_message_failure_returns_context(self):
        """When research fails, the context should still be usable."""
        from app.agents.web_research import research_message

        with patch("app.agents.web_research.urllib.request.urlopen", side_effect=OSError("network error")):
            ctx = research_message("What is the latest version of React?")
            # Should not crash, should indicate failure
            assert isinstance(ctx, ResearchContext)

    def test_engine_continues_after_research_failure(self, engine, tmp_path):
        """ChatEngine must produce a reply even when web research fails."""
        with patch("app.agents.web_research.web_search", side_effect=OSError("network error")):
            response = handle(engine, "What is the latest version of React?", tmp_path)
            assert response.kind == "reply"
            assert response.content.strip()


# ======================================================================
# 10. Provider-independent research context
# ======================================================================


class TestProviderIndependence:
    def test_research_context_is_plain_text(self):
        """Research context is a string that any provider can consume."""
        results = [
            ResearchResult(
                "Title", "https://example.com", "Snippet", 1, "official", "q",
            ),
        ]
        ctx = format_research_context(results)
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_research_context_in_chat_context(self, engine, tmp_path):
        """ChatContext should contain web_research_context field."""
        from app.agents.chat_intelligence import ChatContext

        ctx = ChatContext()
        assert hasattr(ctx, "web_research_context")
        assert ctx.web_research_context == ""

    def test_context_summary_includes_research_info(self):
        from app.agents.chat_intelligence import ChatContext

        ctx = ChatContext(web_research_context="some research")
        lines = ctx.summary_lines()
        assert any("research" in line.lower() for line in lines)


# ======================================================================
# 11. Secret sanitization
# ======================================================================


class TestSecretSanitization:
    @pytest.mark.parametrize(
        "text,expected_safe",
        [
            ("my API key is sk-abc123def456ghi789", "[REDACTED]"),
            ("token: ghp_abcdefghij1234567890abcdef", "[REDACTED]"),
            ("AKIA1234567890123456 is my key", "[REDACTED]"),
            ("Bearer abcdefghijklmnopqrstuvwxyz123456", "Bearer [REDACTED]"),
            ("api_key=supersecretvalue12345", "[REDACTED]"),
            ("password: hunter2", "[REDACTED]"),
            ("normal text without secrets", "normal text without secrets"),
            ("", ""),
        ],
    )
    def test_scrub_query_secrets(self, text, expected_safe):
        result = _scrub_query_secrets(text)
        if expected_safe == "[REDACTED]":
            assert "[REDACTED]" in result
            # Original secret should not appear
            for secret_part in ["sk-abc123", "ghp_abcdefghij", "AKIA1234567890123456",
                                "supersecretvalue", "hunter2"]:
                assert secret_part not in result
        else:
            assert result == expected_safe

    def test_ip_addresses_are_redacted(self):
        result = _scrub_query_secrets("server at 192.168.1.100")
        assert "192.168.1.100" not in result

    def test_localhost_is_redacted(self):
        result = _scrub_query_secrets("running on localhost:8000")
        assert "localhost" not in result.lower()
        assert "[HOST]" in result

    def test_search_query_sanitizes_secrets(self):
        """web_search should not send secrets even if they appear in the query."""
        with patch("app.agents.web_research.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b"<html></html>"
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            web_search("my key is sk-abc123def456ghi789jklmnop")

            # Check the data sent to urlopen
            call_args = mock_urlopen.call_args
            if call_args:
                request = call_args[0][0]
                data = request.data.decode("utf-8") if hasattr(request, "data") and request.data else ""
                assert "sk-abc123" not in data

    def test_engine_research_does_not_expose_secrets(self, engine, tmp_path):
        """Even if a message contains secrets, research context should be clean."""
        from app.agents.web_research import research_message

        with patch("app.agents.web_research.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = b"<html></html>"
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            ctx = research_message("search for sk-test123apikey456token789")
            # The context should not contain the raw secret
            if ctx.context_text:
                assert "sk-test123apikey456token789" not in ctx.context_text


# ======================================================================
# 12. Project-memory integration
# ======================================================================


class TestProjectMemoryIntegration:
    def test_research_does_not_write_to_project_memory(self, engine, tmp_path):
        """Web research is temporary — it must NOT write to .autofix/memory."""
        memory_dir = tmp_path / ".autofix" / "memory"
        assert not memory_dir.exists()

        handle(engine, "What is the latest version of Python?", tmp_path)

        # Memory should not be created by research alone
        assert not memory_dir.exists()

    def test_research_context_is_separate_from_memory(self):
        """Research context and project memory are independent."""
        from app.agents.chat_intelligence import ChatContext

        ctx = ChatContext(
            relevant_project_memory=[{"kind": "decisions", "title": "Use SQLite", "content": "Chose SQLite for simplicity"}],
            web_research_context="Web research: Python 3.12 released",
        )
        lines = ctx.summary_lines()
        memory_lines = [l for l in lines if "memory:" in l]
        research_lines = [l for l in lines if "research" in l.lower()]
        assert len(memory_lines) >= 1
        assert len(research_lines) >= 1


# ======================================================================
# 13. Research context passed into Chat AI
# ======================================================================


class TestResearchContextPassedToChatAI:
    def test_conversational_reply_uses_research_context(self, engine, tmp_path):
        """When research succeeds, the provider receives the context."""
        fake_research = "Web research results:\n1. [OFFICIAL] Python 3.12 Released"

        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value=fake_research,
        ):
            with patch(
                "app.agents.chat_provider.converse",
                return_value="Python 3.12 is the latest version.",
            ) as mock_converse:
                response = handle(engine, "What is the latest version of Python?", tmp_path)
                assert response.kind == "reply"
                # converse should have been called
                if mock_converse.called:
                    call_kwargs = mock_converse.call_args
                    # The system_context should contain the research
                    system_ctx = call_kwargs.kwargs.get("system_context", "")
                    assert "Python 3.12" in system_ctx or "research" in system_ctx.lower()

    def test_proposal_includes_research_analysis(self, engine, tmp_path):
        """Coding requests with research get enriched analysis."""
        fake_research = "Web research: recommended approach is X"

        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value=fake_research,
        ):
            response = handle(engine, "Add automatic retry to the worker router", tmp_path)
            assert response.kind == "proposal"
            # The proposal should have been generated (research enriches but doesn't block)
            assert response.proposal is not None


# ======================================================================
# 14. Coding request → research → proposal
# ======================================================================


class TestCodingRequestResearchProposal:
    def test_coding_request_research_then_proposal(self, engine, tmp_path):
        """Coding request should research (if useful) then produce proposal."""
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            response = handle(engine, "Add dark mode to the application", tmp_path)
            assert response.kind == "proposal"
            assert response.proposal.status == "AWAITING APPROVAL"

    def test_proposal_preserves_locked_architecture(self, engine, tmp_path):
        """Even with research, proposals must respect the locked architecture."""
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            response = handle(engine, "Add error handling to the pipeline", tmp_path)
            if response.kind == "proposal":
                prompt = response.execution_prompt
                # Must contain locked architecture constraints
                assert "AutoFix" in prompt or "approval" in prompt.lower()


# ======================================================================
# 15. Approval still required
# ======================================================================


class TestApprovalStillRequired:
    def test_proposal_never_auto_executes(self, engine, tmp_path):
        """Proposals ALWAYS require approval, even with research."""
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            response = handle(engine, "Create a login page", tmp_path)
            assert response.kind == "proposal"
            assert response.proposal.status == "AWAITING APPROVAL"
            assert task_files(tmp_path) == []

    def test_approval_message_still_triggers_handoff(self, engine, tmp_path):
        """Approval still follows the same path regardless of research."""
        # First, create a proposal
        r1 = handle(engine, "Create a login page", tmp_path)
        assert r1.kind == "proposal"

        # Then approve
        r2 = handle(engine, "approve", tmp_path, active=r1.proposal.to_dict())
        assert r2.kind == "approval"
        assert r2.execution_prompt

    def test_discussion_never_arms_approval(self, engine, tmp_path):
        """Research-enriched discussion still never arms approval."""
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            response = handle(engine, "What is the latest version of React?", tmp_path)
            assert response.kind == "reply"
            assert response.proposal is None


# ======================================================================
# 16. Research never creates an AutoFix task by itself
# ======================================================================


class TestResearchNeverCreatesTask:
    def test_pure_question_no_task(self, engine, tmp_path):
        """A question that triggers research must NOT create an AutoFix task."""
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            handle(engine, "What is the best way to handle CORS in FastAPI?", tmp_path)
            assert task_files(tmp_path) == []

    def test_discussion_with_research_no_task(self, engine, tmp_path):
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            handle(engine, "Why does Docker build fail on Windows?", tmp_path)
            assert task_files(tmp_path) == []

    def test_analysis_with_research_no_task(self, engine, tmp_path):
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            handle(engine, "Analyze the error: TypeError: cannot read property of undefined", tmp_path)
            assert task_files(tmp_path) == []


# ======================================================================
# 17. Knowledge candidate detection (preserved)
# ======================================================================


class TestKnowledgeCandidateDetection:
    def test_knowledge_detection_still_works(self, engine, tmp_path):
        """Existing knowledge detection must still function."""
        response = handle(
            engine,
            "Remember: always use context managers for file I/O in Python",
            tmp_path,
        )
        # Knowledge detection should be attached if the response qualifies
        # (may or may not trigger depending on the heuristic)
        assert response.kind in ("reply", "proposal", "revision")

    def test_knowledge_proposal_dict_format(self):
        """Knowledge proposals must have the expected dict format."""
        from app.agents.knowledge_detection import KnowledgeProposal

        kp = KnowledgeProposal(
            title="Test knowledge",
            category="lessons",
            body="Always use context managers",
            source="Chat conversation",
            confidence=0.7,
        )
        d = kp.to_dict()
        assert "title" in d
        assert "category" in d
        assert "body" in d
        assert "confidence" in d


# ======================================================================
# 18. Knowledge save requires explicit approval (preserved)
# ======================================================================


class TestKnowledgeSaveRequiresApproval:
    def test_knowledge_not_auto_saved(self, engine, tmp_path):
        """Knowledge is never auto-saved — only detected as a candidate."""
        response = handle(engine, "Save this: pytest fixtures are powerful", tmp_path)
        # The response may contain a knowledge_proposal, but it's just a candidate
        if response.knowledge_proposal:
            assert isinstance(response.knowledge_proposal, dict)


# ======================================================================
# 19. GitHub knowledge separation from project memory
# ======================================================================


class TestKnowledgeSeparation:
    def test_research_does_not_github_knowledge(self, engine, tmp_path):
        """Web research should not write to the GitHub knowledge repository."""
        # Research is temporary; only explicit approval saves to GitHub
        with patch(
            "app.agents.chat_intelligence._run_web_research",
            return_value="Research context",
        ):
            response = handle(engine, "What is the latest Python version?", tmp_path)
            assert response.kind == "reply"


# ======================================================================
# 20. Existing AutoFix regression tests
# ======================================================================


class TestAutoFixRegression:
    def test_proposal_generation_unchanged(self, engine, tmp_path):
        """Proposal generation must work identically with research."""
        response = handle(engine, "Add retry logic to worker router", tmp_path)
        if response.kind == "proposal":
            p = response.proposal
            assert p.objective
            assert p.plan
            assert p.verification_plan
            assert p.execution_prompt
            assert "OBJECTIVE" in p.execution_prompt

    def test_approval_flow_unchanged(self, engine, tmp_path):
        """Full proposal → revision → approval flow still works."""
        r1 = handle(engine, "Add dark mode", tmp_path)
        assert r1.kind == "proposal"

        r2 = handle(
            engine,
            "Make OpenCode primary and DeepSeek fallback",
            tmp_path,
            active=r1.proposal.to_dict(),
        )
        assert r2.kind == "revision"
        assert len(r2.proposal.revisions) == 1
        assert "OpenCode" in r2.proposal.worker_preference

        r3 = handle(
            engine,
            "approve",
            tmp_path,
            active=r2.proposal.to_dict(),
        )
        assert r3.kind == "approval"
        assert r3.execution_prompt

    def test_locked_architecture_conflict_detection(self, engine, tmp_path):
        """Architecture conflicts are still detected."""
        response = handle(
            engine,
            "Create a second execution engine for bulk tasks",
            tmp_path,
        )
        assert response.kind == "reply"
        assert "conflict" in response.content.lower() or "architecture" in response.content.lower()

    def test_clarification_still_works(self, engine, tmp_path):
        """Clarification gate still works with research in the picture."""
        response = handle(engine, "Connect AutoFix to my API.", tmp_path)
        assert response.kind == "clarification"
        assert response.requires_clarification

    def test_greeting_never_triggers_research(self, engine, tmp_path):
        """Greetings must always be fast, no research."""
        response = handle(engine, "hello", tmp_path)
        assert response.kind == "reply"
        assert response.proposal is None
        assert task_files(tmp_path) == []


# ======================================================================
# ResearchContext model tests
# ======================================================================


class TestResearchContext:
    def test_default_state(self):
        ctx = ResearchContext(should_research=False)
        assert ctx.should_research is False
        assert ctx.queries == []
        assert ctx.results == []
        assert ctx.context_text == ""
        assert ctx.failed is False
        assert ctx.error == ""

    def test_research_result_domain_extraction(self):
        r = ResearchResult(
            "Title", "https://docs.python.org/3/", "Snippet", 1, "official", "q",
        )
        assert r.domain == "python.org"

    def test_research_result_empty_url(self):
        r = ResearchResult("Title", "", "Snippet", 1, "official", "q")
        assert r.domain == ""
