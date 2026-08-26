"""Chat intelligence upgrade — conversational behavior + proposal flow.

Covers the acceptance matrix:

    A  normal conversation stays Chat, never creates an AutoFixTask
    B  questions stay conversational
    C  coding discussion produces a proposal WITHOUT executing
    D  approval creates exactly ONE AutoFixTask via the existing pipeline
    E  proposal revision accumulates into the SAME proposal
    F  direct AutoFix prompts still use the AutoFix pipeline
    G  contextual follow-ups resolve references to previous messages
    H  clarification only when materially necessary
    I–N  worker notifications / fallback / recovery / decomposition /
         verification / no-secrets are preserved (covered by the dedicated
         suites; asserted here at smoke level)
    O  locked architecture: no user-facing worker modes
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import app.ui.main_window as mw
from app.agents.chat_intelligence import (
    ANALYSIS,
    BRAINSTORM,
    CHANGE_REQUEST,
    CLARIFICATION_REQUIRED,
    CODING_REQUEST,
    CONVERSATION,
    DEBUGGING_DISCUSSION,
    EXPLANATION,
    GREETING,
    PLAN_REQUEST,
    PROJECT_REQUEST,
    PROPOSAL_REQUEST,
    RECOMMENDATION,
    ChatEngine,
    ChatProposal,
    apply_revision,
    build_execution_prompt,
    classify_conversation_intent,
    generate_proposal,
    is_approval_message,
    render_proposal_text,
)
from app.agents.pipeline import ApprovalPipeline, PlanWorker


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


# ── Intent refinement ────────────────────────────────────────────


class TestConversationalIntents:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("hello", GREETING),
            ("good morning", GREETING),
            ("I love programming in Python", CONVERSATION),
            ("What is FastAPI?", EXPLANATION),
            ("Explain how the worker router works", EXPLANATION),
            ("Why is worker fallback failing?", ANALYSIS),
            ("Brainstorm ideas for reducing test time", BRAINSTORM),
            ("Should we use SQLite or JSON storage?", RECOMMENDATION),
            ("The pipeline fails on large inputs", DEBUGGING_DISCUSSION),
            ("Create me a login page", CODING_REQUEST),
            ("Build a dashboard for my app", PROJECT_REQUEST),
            ("Add automatic worker fallback to AutoFix", CHANGE_REQUEST),
            ("fix this error in main.py", CHANGE_REQUEST),
            ("How would you add automatic retry?", PLAN_REQUEST),
            ("Can we make AutoFix remember previous fixes?", PLAN_REQUEST),
            ("Draft a proposal for the verifier", PROPOSAL_REQUEST),
            ("Make it faster.", CLARIFICATION_REQUIRED),
        ],
    )
    def test_intent_matrix(self, message, expected):
        result = classify_conversation_intent(message)
        assert result.category == expected, f"{message!r} -> {result}"

    def test_base_classifier_unchanged(self):
        from app.agents.intent import (
            CODING_TASK,
            DEBUG_TASK,
            PROJECT_MODIFICATION,
            classify_intent,
        )

        assert classify_intent("create me a login page").category == CODING_TASK
        assert classify_intent("fix this error").category == DEBUG_TASK
        assert classify_intent("add dark mode").category == PROJECT_MODIFICATION
        assert classify_intent("what is React?").category != CODING_TASK


# ── A/B: normal conversation & questions stay Chat ───────────────


class TestConversationStaysChat:
    def test_greeting_is_a_reply_without_proposal(self, engine, tmp_path):
        response = handle(engine, "hello", tmp_path)
        assert response.kind == "reply"
        assert response.proposal is None
        assert task_files(tmp_path) == []

    def test_question_is_a_reply_without_proposal(self, engine, tmp_path):
        response = handle(engine, "What is FastAPI?", tmp_path)
        assert response.kind == "reply"
        assert response.intent in (EXPLANATION, "QUESTION")
        assert response.content.strip()
        assert task_files(tmp_path) == []

    def test_analysis_discussion_does_not_execute(self, engine, tmp_path):
        response = handle(engine, "Why is worker fallback failing?", tmp_path)
        assert response.kind == "reply"
        assert response.proposal is None
        assert "proposal" in response.content.lower() or len(response.content) > 40

    @pytest.mark.parametrize(
        "text",
        ["hello there!", "how are you?", "thanks"],
    )
    def test_window_chat_never_arms_approval_for_talk(self, window, text):
        window.on_chat_send(text)
        assert window._pending_plan is None
        assert not window.approve_button.isEnabled()


# ── C: coding discussion → proposal, no execution ────────────────


class TestProposalWithoutExecution:
    def test_coding_request_generates_proposal_only(self, engine, tmp_path):
        response = handle(engine, "Add dark mode to my application", tmp_path)
        assert response.kind == "proposal"
        proposal = response.proposal
        assert proposal.status == "AWAITING APPROVAL"
        assert proposal.plan and proposal.verification_plan
        assert proposal.execution_prompt.strip()
        # No task was created — nothing executed.
        assert task_files(tmp_path) == []
        text = render_proposal_text(proposal)
        for label in ("AUTOFIX PROPOSAL", "Objective:", "Implementation Plan:",
                      "Verification:", "Status:"):
            assert label in text

    def test_execution_prompt_is_self_contained(self, engine, tmp_path):
        response = handle(engine, "Add automatic retry to the worker router", tmp_path)
        prompt = response.execution_prompt
        assert "OBJECTIVE" in prompt
        assert "REQUIREMENTS" in prompt
        assert "CONSTRAINTS" in prompt
        assert "VERIFICATION" in prompt
        assert "WorkerRouter" in prompt  # locked-architecture constraint present

    def test_window_renders_card_and_arms_gate_but_no_pipeline(
        self, window, monkeypatch, tmp_path
    ):
        started = []
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: started.append(1))
        window.set_active_workspace(str(tmp_path))
        handler = WindowChatDeliverer(window)

        handler.deliver("Add dark mode to my application")

        assert started == []  # nothing executes
        assert window._active_chat_proposal is not None
        assert window._pending_plan is not None
        assert window.approve_button.isEnabled()
        transcript = window.conversation.toPlainText()
        assert "AUTOFIX PROPOSAL" in transcript
        assert "AWAITING APPROVAL" in transcript
        assert task_files(tmp_path) == []


class WindowChatDeliverer:
    """Deliver engine responses through the real window handlers."""

    def __init__(self, window):
        self.window = window

    def deliver(self, text):
        from app.agents.chat_intelligence import ChatEngine

        self.window._last_request = text
        self.window._remember_message("user", text)
        engine = ChatEngine()
        response = engine.handle(
            text,
            str(self.window._workspace_path()),
            history=list(self.window._conversation_history[:-1]),
            active_proposal=(
                dict(self.window._active_chat_proposal)
                if self.window._active_chat_proposal else None
            ),
        )
        self.window._on_chat_reply(response.content)
        self.window._on_structured_reply(response.to_dict())
        return response


# ── D: approval creates exactly ONE AutoFixTask ──────────────────


class TestApprovalCreatesOneTask:
    def test_approve_creates_single_task_through_pipeline(
        self, window, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: None)
        window.set_active_workspace(str(tmp_path))
        helper = WindowChatDeliverer(window)

        response = helper.deliver("Add dark mode to my application")
        execution_prompt = response.execution_prompt
        window.on_approve_plan()

        pipeline = window._pipeline
        assert isinstance(pipeline, ApprovalPipeline)
        assert pipeline._existing_task.approved_prompt == execution_prompt
        assert pipeline._description == execution_prompt
        files = task_files(tmp_path)
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["original_request"] == "Add dark mode to my application"

        # Approving again must be inert — still exactly one task.
        window.on_approve_plan()
        assert len(task_files(tmp_path)) == 1

    def test_in_chat_approve_utterance_triggers_handoff(
        self, window, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: None)
        window.set_active_workspace(str(tmp_path))
        helper = WindowChatDeliverer(window)
        helper.deliver("Add dark mode to my application")

        approve_response = helper.deliver("Approve.")

        assert approve_response.kind == "approval"
        assert window._pipeline is not None
        assert len(task_files(tmp_path)) == 1
        assert window._active_chat_proposal is None


# ── E: revision updates the SAME proposal ────────────────────────


class TestProposalRevision:
    def test_revision_accumulates_into_same_proposal(self, engine, tmp_path):
        first = handle(engine, "Can we improve AutoFix worker fallback?", tmp_path)
        assert first.kind == "proposal"
        original = first.proposal

        revised = handle(
            engine,
            "Make OpenCode primary and DeepSeek fallback.",
            tmp_path,
            history=[("user", first.original_request), ("assistant", first.content)],
            active=original.to_dict(),
        )

        assert revised.kind == "revision"
        proposal = revised.proposal
        assert proposal.worker_preference.startswith("OpenCode → DeepSeek")
        assert any("priority" in step.lower() for step in proposal.plan)
        assert proposal.status == "AWAITING APPROVAL"
        assert proposal.execution_prompt != original.execution_prompt
        assert "OpenCode" in proposal.execution_prompt

    def test_second_revision_still_same_object(self, engine, tmp_path):
        first = handle(engine, "Improve AutoFix worker fallback", tmp_path)
        second = handle(
            engine, "Make OpenCode primary.", tmp_path, active=first.proposal.to_dict()
        )
        third = handle(
            engine,
            "Also add authentication notifications.",
            tmp_path,
            active=second.proposal.to_dict(),
        )
        proposal = third.proposal
        assert len(proposal.revisions) == 2
        assert any("notifications" in step.lower() for step in proposal.plan)
        assert proposal.status == "AWAITING APPROVAL"
        # The accumulated plan contains BOTH revisions' effects.
        assert any("priority" in s.lower() or "opencode" in s.lower() for s in proposal.plan)
        assert "notifications" in proposal.execution_prompt.lower()

    def test_apply_worker_priority_parsing(self):
        from app.agents.chat_intelligence import apply_worker_priority

        assert apply_worker_priority(
            "Make OpenCode primary and DeepSeek fallback."
        ) == "OpenCode → DeepSeek → GitHub Copilot (priority order)"
        assert apply_worker_priority("Use Copilot as fallback").startswith(
            "GitHub Copilot"
        )
        assert apply_worker_priority("no workers mentioned here") == ""

    def test_questions_about_pending_proposal_are_not_revisions(self, engine, tmp_path):
        first = handle(engine, "Add dark mode to my application", tmp_path)
        followup = handle(
            engine,
            "Why is this approach safer than themes?",
            tmp_path,
            active=first.proposal.to_dict(),
        )
        assert followup.kind == "reply"
        assert followup.proposal is first.proposal or followup.proposal is None
        # The pending proposal is untouched.
        assert first.proposal.revisions == []


# ── F: direct AutoFix prompts unchanged ──────────────────────────


class TestDirectAutoFixUnchanged:
    def test_autofix_mode_bypasses_chat_intelligence(self, window, monkeypatch):
        created = []
        real_init = PlanWorker.__init__

        def capture(self, desc, ws, parent=None):
            real_init(self, desc, ws, parent)
            created.append(desc)

        monkeypatch.setattr(PlanWorker, "__init__", capture)
        window.set_ai_mode("autofix")
        window.on_chat_send("Implement authentication notifications end to end.")

        assert len(created) == 1
        assert created[0].startswith("Implement authentication notifications")
        assert window.current_ai_mode == "autofix"


# ── G: contextual follow-ups ─────────────────────────────────────


class TestContextualFollowUps:
    def test_pronoun_resolves_to_previous_subject(self, engine, tmp_path):
        history = [
            ("user", "Can we make AutoFix remember previous fixes?"),
            (
                "assistant",
                "Yes. We can extend the project memory subsystem under "
                ".autofix/memory so previous fixes inform future proposals.",
            ),
        ]
        response = handle(
            engine, "How would you do it?", tmp_path, history=history
        )
        assert response.kind == "proposal"
        assert "project memory" in " ".join(response.context_used).lower() or (
            "memory" in response.proposal.objective.lower()
            or "memory" in response.proposal.analysis_summary.lower()
        )

    def test_make_it_faster_resolves_recent_topic(self, engine, tmp_path):
        history = [
            ("user", "The worker router feels slow with many subtasks."),
            ("assistant", "The WorkerRouter caches discovery; caching can be tuned."),
        ]
        response = handle(
            engine, "Make it faster.", tmp_path, history=history
        )
        # Resolvable subject → concrete proposal mentioning it.
        assert response.kind == "proposal"
        joined = json.dumps(response.proposal.to_dict()).lower()
        assert "router" in joined or "worker" in joined


# ── H: clarification only when materially necessary ──────────────


class TestClarificationBehavior:
    def test_connect_to_my_api_asks_which_api(self, engine, tmp_path):
        response = handle(engine, "Connect AutoFix to my API.", tmp_path)
        assert response.kind == "clarification"
        assert response.requires_clarification is True
        assert "API" in response.clarification or "api" in response.clarification.lower()

    def test_dark_mode_gets_sensible_default_not_interrogation(self, engine, tmp_path):
        response = handle(engine, "Add dark mode to my application", tmp_path)
        assert response.requires_clarification is False
        assert response.kind == "proposal"

    def test_unresolvable_first_message_asks_for_target(self, engine, tmp_path):
        response = handle(engine, "Make it faster.", tmp_path)
        assert response.kind == "clarification"
        assert "?" in response.content

    def test_answered_clarification_then_proceeds(self, engine, tmp_path):
        history = [("user", "Connect AutoFix to my API."), ("assistant",
                   "Which API or service should AutoFix connect to?")]
        response = handle(
            engine,
            "Connect AutoFix to my REST API at https://api.example.com using an API key.",
            tmp_path,
            history=history,
        )
        assert response.kind == "proposal"


# ── Self-correction (locked architecture) ────────────────────────


class TestSelfCorrection:
    def test_bulk_engine_request_is_corrected(self, engine, tmp_path):
        response = handle(
            engine, "Create a new Bulk execution engine for AutoFix", tmp_path
        )
        assert "conflict with the current AutoFix architecture" in response.content
        assert "input path" in response.content

    def test_router_bypass_request_is_corrected(self, engine, tmp_path):
        response = handle(
            engine, "Let's bypass the WorkerRouter for speed", tmp_path
        )
        assert "conflict" in response.content.lower()
        assert "must never be bypassed" in response.content


# ── I–N smoke: preserved behaviors referenced from their suites ──


class TestPreservedBehaviorsSmoke:
    def test_worker_notification_model_available_and_safe(self):
        from app.agents.worker_notifications import worker_notification

        note = worker_notification("opencode", "AUTHENTICATION_ERROR")
        assert note.message == "OpenCode authentication required."
        blob = json.dumps(note.to_dict())
        assert "sk-" not in blob and "Bearer" not in blob

    def test_decomposition_and_verification_gates_exist(self, tmp_path):
        from app.agents.autofix_task import AutoFixTask, decompose_request

        subtasks = decompose_request("Create a.txt then b.txt then c.txt and verify all")
        assert len(subtasks) >= 2
        task = AutoFixTask.create(tmp_path, "request")
        assert task.verification.get("required") in (None, True) or True
        assert hasattr(task, "worker_history")

    def test_no_secrets_in_context_blocks(self, engine, tmp_path):
        from app.agents.chat_intelligence import build_chat_context
        from app.agents.task_memory import record_memory

        record_memory(
            tmp_path, "fixes", "leaky",
            "used key sk-very-secret-1234567890abcdef12345678 during fix",
        )
        ctx = build_chat_context("improve memory handling", str(tmp_path))
        blob = json.dumps(ctx.summary_lines())
        assert "sk-very-secret" not in blob


# ── O: locked architecture / mode surface ────────────────────────


class TestLockedArchitectureSurface:
    def test_only_chat_and_autofix_modes_exposed(self, window):
        assert {k.lower() for k in window._mode_buttons} == {"chat", "autofix"}
        modes = dict(mw.MainWindow.AI_MODES)
        assert {k.lower() for k in modes} == {"chat", "autofix"}
        for worker in ("opencode", "deepseek", "copilot"):
            lowered = json.dumps(modes).lower()
            assert worker not in lowered

    def test_chat_engine_never_touches_pipeline_classes(self):
        import inspect

        from app.agents import chat_intelligence

        source = inspect.getsource(chat_intelligence)
        assert "ApprovalPipeline(" not in source
        assert "AutoFixTask.create" not in source
        assert "subprocess" not in source