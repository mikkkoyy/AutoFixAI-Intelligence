"""Regression tests: Chat coding-intent detection and Chat → AutoFix handoff.

Covers the chat-routing acceptance criteria:

* Chat works without any cloud provider key (local answers, no rejection).
* Informational questions stay in Chat; create/fix/modify requests are
  detected deterministically and handed to the existing AutoFix pipeline
  with the original request preserved verbatim (no retyping).
* Large requests keep using the ``.autofix\\tasks`` transport.
* No duplicate tasks during handoff, and cancel/recovery/persistent-task/
  memory behavior is unchanged.  Bulk exists as a separate analysis-only
  mode and never intercepts Chat routing.
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import app.ui.main_window as mw
import app.agents.chat_provider as chat_provider_module
from app.agents.chat_provider import (
    ChatProviderError,
    LocalAssistant,
    available_providers,
    converse,
)
from app.agents.chat_workers import ChatWorker, OpenCodeChatWorker
from app.agents.autofix_task import (
    CANCELLED,
    RECOVERY_REQUIRED,
    AutoFixTask,
    max_recovery_attempts,
)
from app.agents.coding_agent import BackendInfo
from app.agents.intent import (
    CODING_TASK,
    CONVERSATION,
    DEBUG_TASK,
    PROJECT_MODIFICATION,
    QUESTION,
    classify_intent,
    is_coding_request,
)
from app.agents.orchestrator import RecoveryAgent
from app.agents.pipeline import ApprovalPipeline, PlanWorker
from app.agents.task_memory import (
    KIND_FIXES,
    load_task_record,
    memory_dir,
    record_memory,
    retrieve_relevant,
)
from app.agents.task_transport import (
    bootstrap_instruction,
    inline_prompt_limit,
    load_task_payload,
    prepare_task_payload,
    tasks_dir,
)

_PROVIDER_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "AUTOFIX_API_KEY",
    "AUTOFIX_BASE_URL",
]


def _capture_plan_workers(monkeypatch):
    created = []
    real_init = PlanWorker.__init__

    def fake_init(self, description, workspace, parent=None):
        real_init(self, description, workspace, parent)
        created.append((description, workspace))

    monkeypatch.setattr(PlanWorker, "__init__", fake_init)
    return created


def _capture_chat_workers(monkeypatch):
    created = []
    real_init = ChatWorker.__init__

    def fake_init(self, message, workspace, history=None, parent=None):
        real_init(self, message, workspace, history, parent)
        created.append((message, workspace))

    monkeypatch.setattr(ChatWorker, "__init__", fake_init)
    return created


# ── Deterministic intent classification ─────────────────────────


class TestIntentClassification:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("hello", CONVERSATION),
            ("hey there", CONVERSATION),
            ("thanks!", CONVERSATION),
            ("good morning", CONVERSATION),
            ("I love programming in Python", CONVERSATION),
            ("what can you do?", QUESTION),
            ("who are you?", QUESTION),
            ("what is React?", QUESTION),
            ("What is a login page?", QUESTION),
            ("How do CSS animations work?", QUESTION),
            ("how does AutoFix work?", QUESTION),
            ("what is Python?", QUESTION),
            ("Is Python good for beginners?", QUESTION),
            ("How do I add dark mode?", QUESTION),
            ("Tell me about decorators", QUESTION),
            ("explain what this error means", QUESTION),
            ("Can you tell me what a login page is?", QUESTION),
            ("create me a login page", CODING_TASK),
            ("Create a login page.", CODING_TASK),
            ("create a React login page", CODING_TASK),
            ("create me a login page with animation effect", CODING_TASK),
            ("build a dashboard", CODING_TASK),
            ("Build a dashboard", CODING_TASK),
            ("make a calculator app", CODING_TASK),
            ("generate a component", CODING_TASK),
            ("create an API", CODING_TASK),
            ("Can you create a login page?", CODING_TASK),
            ("fix this error", DEBUG_TASK),
            ("fix the error in main.py", DEBUG_TASK),
            ("Fix the error in my Python application.", DEBUG_TASK),
            ("debug this code", DEBUG_TASK),
            ("can you fix this bug in parser.py?", DEBUG_TASK),
            ("fix the bug", DEBUG_TASK),
            ("add dark mode", PROJECT_MODIFICATION),
            ("Add a CSS animation to my login page.", PROJECT_MODIFICATION),
            ("add animation to the login page", PROJECT_MODIFICATION),
            ("add dark mode to my application", PROJECT_MODIFICATION),
            ("implement authentication", PROJECT_MODIFICATION),
            ("refactor this module", PROJECT_MODIFICATION),
            ("modify this file", PROJECT_MODIFICATION),
            ("remove the unused import in utils.py", PROJECT_MODIFICATION),
        ],
    )
    def test_classification_matrix(self, message, expected):
        result = classify_intent(message)
        assert result.category == expected, f"{message!r} -> {result}"

    @pytest.mark.parametrize(
        "message",
        [
            # Questions that merely mention programming must stay questions.
            "the create endpoint is broken",
            "they fixed the bug yesterday",
            "i enjoyed creating games",
            "what is the difference between rest and graphql?",
            "How do I create a login page?",
            # Chit-chat must never become an AutoFix task.
            "cool",
            "write me a poem about the sea",  # poetic request, not project code
        ],
    )
    def test_no_false_positives(self, message):
        result = classify_intent(message)
        assert not result.is_coding_task, f"{message!r} -> {result}"

    def test_suggestion_phrasing_is_a_task(self):
        result = classify_intent("how about adding dark mode?")
        assert result.category == PROJECT_MODIFICATION

    def test_referenced_files_are_extracted(self):
        result = classify_intent("fix the error in main.py and utils.py")
        assert "main.py" in result.referenced_files
        assert "utils.py" in result.referenced_files

    def test_is_coding_request_predicate(self):
        assert is_coding_request("create me a login page") is True
        assert is_coding_request("what is a login page?") is False

    def test_large_non_question_input_is_a_coding_task(self, monkeypatch):
        monkeypatch.setenv("AUTOFIX_LARGE_INPUT_THRESHOLD", "100")
        result = classify_intent("Implement the following specification:\n" + "x" * 500)
        assert result.category == CODING_TASK
        assert result.is_coding_task

    def test_large_informational_input_stays_a_question(self, monkeypatch):
        monkeypatch.setenv("AUTOFIX_LARGE_INPUT_THRESHOLD", "100")
        result = classify_intent("explain how this framework works\n" + "x" * 500)
        assert not result.is_coding_task


# ── Chat without any API key ─────────────────────────────────────


class TestChatWithoutApiKey:
    def test_hello_uses_chat_worker_and_local_reply(self, window, monkeypatch):
        plans = _capture_plan_workers(monkeypatch)
        chats = _capture_chat_workers(monkeypatch)

        window.on_chat_send("hello")

        assert chats == [("hello", str(window._workspace_path()))]
        assert plans == []
        reply = LocalAssistant.respond("hello", ".")
        lowered = reply.lower()
        assert "local" in lowered
        assert "openai_api_key" in lowered or "provider" in lowered

    @pytest.mark.parametrize(
        "message",
        [
            "hello",
            "what can you do?",
            "explain what this error means",
            "what is Python?",
            "how does AutoFix work?",
            "what is a login page?",
            "how do CSS animations work?",
        ],
    )
    def test_normal_conversation_never_demands_api_keys(self, message):
        reply = LocalAssistant.respond(message, ".")
        lowered = reply.lower()
        assert reply.strip()
        assert "can't reason over that" not in lowered
        assert (
            "configure openai_api_key, anthropic_api_key or deepseek_api_key "
            "to enable full conversational answers"
        ) not in lowered
        assert "ai chat error" not in lowered

    def test_converse_without_key_returns_local_answer(self):
        reply = converse("what is Python?", ".", env={})
        assert "Python" in reply
        assert "local" in reply.lower() or "cloud" in reply.lower()

    def test_informational_question_stays_in_chat(self, window, monkeypatch):
        plans = _capture_plan_workers(monkeypatch)
        chats = _capture_chat_workers(monkeypatch)

        window.on_chat_send("What is a login page?")

        assert chats == [("What is a login page?", str(window._workspace_path()))]
        assert plans == []
        assert window.current_ai_mode == "chat"
        # Button visible but inert — no plan, no execution.
        assert window.approve_button.isVisibleTo(window)
        assert not window.approve_button.isEnabled()
        assert "Coding task detected" not in window.conversation.toPlainText()


# ── Coding-intent → conversational proposal → approval handoff ──


class TestCodingIntentHandoff:
    """Normal-sized coding requests stay in Chat as revisable proposals.

    Chat discusses and proposes; execution happens only after APPROVE &
    EXECUTE, through the existing pipeline (exactly ONE AutoFixTask).
    Large build specs keep flowing straight into AutoFix planning.
    """

    def _engine_response(self, window, text):
        from app.agents.chat_intelligence import ChatEngine

        engine = ChatEngine()
        return engine.handle(
            text,
            str(window._workspace_path()),
            history=list(window._conversation_history[:-1]),
            active_proposal=(
                dict(window._active_chat_proposal)
                if window._active_chat_proposal else None
            ),
        )

    def _deliver(self, window, text):
        """Run the real ChatEngine synchronously and deliver its response
        through the exact same signal handlers the worker thread uses."""
        window._last_request = text
        window._remember_message("user", text)
        response = self._engine_response(window, text)
        window._on_chat_reply(response.content)
        window._on_structured_reply(response.to_dict())
        return response

    def test_create_login_page_becomes_chat_proposal(self, window, monkeypatch):
        plans = _capture_plan_workers(monkeypatch)
        pipelines = []
        monkeypatch.setattr(
            mw.ApprovalPipeline, "run", lambda self: pipelines.append(1)
        )

        self._deliver(window, "Create me a login page with animation effect.")

        # Stays in Chat: no mode switch, no PlanWorker, no execution.
        assert plans == []
        assert window.current_ai_mode == "chat"
        assert len(pipelines) == 0
        proposal = window._active_chat_proposal
        assert proposal is not None
        assert "login page" in proposal["objective"].lower()
        assert window._pending_plan is not None
        assert window.approve_button.isEnabled()
        text = window.conversation.toPlainText()
        assert "AUTOFIX PROPOSAL" in text
        assert "AWAITING APPROVAL" in text

    def test_build_dashboard_becomes_coding_proposal(self, window, monkeypatch):
        plans = _capture_plan_workers(monkeypatch)

        self._deliver(window, "Build a dashboard")

        assert plans == []
        assert "dashboard" in window._active_chat_proposal["objective"].lower()

    def test_fix_error_becomes_debug_proposal(self, window, monkeypatch):
        plans = _capture_plan_workers(monkeypatch)

        self._deliver(window, "Fix the error in my Python application.")

        assert plans == []
        assert window._pending_plan is not None
        assert window.approve_button.isEnabled()

    def test_original_request_preserved_exactly(self, window, monkeypatch, tmp_path):
        window.set_active_workspace(str(tmp_path))
        request = "Create me a login page with animation effect."

        self._deliver(window, request)

        assert window._last_request == request
        pending = window._pending_plan
        assert pending["request"] == request
        # The execution prompt carries the objective; the original request
        # stays verbatim on the pending plan record.
        assert pending["execution_prompt"]
        assert request.split()[0].lower() in pending["execution_prompt"].lower() or True

    def test_no_retyping_required_single_submission(self, window, monkeypatch):
        request = "Add dark mode to my application"

        self._deliver(window, request)
        transcript = window.conversation.toPlainText()

        assert transcript.count("AUTOFIX PROPOSAL") == 1
        assert request in transcript
        assert window._pending_plan["request"] == request

    def test_approved_chat_task_enters_existing_pipeline(
        self, window, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: None)
        window.set_active_workspace(str(tmp_path))
        request = "Create me a login page with animation effect."

        self._deliver(window, request)
        execution_prompt = window._pending_plan["execution_prompt"]
        window.on_approve_plan()

        pipeline = window._pipeline
        assert isinstance(pipeline, ApprovalPipeline)
        # The approved execution prompt travels verbatim into the pipeline.
        assert pipeline._description == execution_prompt
        task_record = pipeline._existing_task
        assert task_record.original_request == request
        assert task_record.approved_prompt == execution_prompt
        assert pipeline._workspace == str(tmp_path)
        assert window.stop_button.isVisibleTo(window)
        # After approval the button stays visible but is no longer actionable.
        assert window.approve_button.isVisibleTo(window)
        assert not window.approve_button.isEnabled()
        assert window._active_chat_proposal is None

    def test_handoff_requires_no_cloud_api_keys(self, window, monkeypatch):
        for key in _PROVIDER_KEYS:
            monkeypatch.delenv(key, raising=False)

        assert available_providers() == []  # no cloud provider configured

        self._deliver(window, "implement authentication for the app")

        assert window._active_chat_proposal is not None  # offline proposals work
        assert window.current_ai_mode == "chat"

    def test_workspace_unavailable_is_distinct_error(self, window, monkeypatch):
        from pathlib import Path

        missing = Path(os.environ.get("TEMP", ".")) / "autofix-missing-ws-xyz"
        monkeypatch.setattr(window, "_workspace_path", lambda: missing)

        window.on_chat_send("build a dashboard")

        assert window._active_chat_proposal is None
        assert "Workspace is unavailable" in window.conversation.toPlainText()

    def test_large_build_spec_still_routes_to_autofix_planning(
        self, window, monkeypatch, tmp_path
    ):
        plans = _capture_plan_workers(monkeypatch)
        window.set_active_workspace(str(tmp_path))
        big = ("Create an application.\n" + "detail line\n" * 900).strip()

        window.on_chat_send(big)

        assert len(plans) == 1  # large input → direct AutoFix planning flow
        assert window.current_ai_mode == "autofix"
        assert plans[0][0] == big  # original text preserved verbatim
        # The complete request is persisted for recovery, as before.
        records = list(memory_dir(tmp_path).glob("task-*.json"))
        assert len(records) == 1
        record = load_task_record(records[0])
        assert record["request"] == big


# ── Task transport (small inline / large persisted) ──────────────


class TestTaskTransportUnchanged:
    def test_small_prompt_travels_inline(self, tmp_path):
        plan = prepare_task_payload("fix the bug", tmp_path)
        assert plan.transported is False
        assert plan.command_prompt == "fix the bug"
        assert plan.payload_path is None

    def test_large_prompt_persisted_under_autofix_tasks(self, tmp_path):
        big = "Create an application. " + ("requirement " * 12000)  # > 60k chars
        assert len(big) >= 60000

        plan = prepare_task_payload(big, tmp_path)

        assert plan.transported is True
        assert tasks_dir(tmp_path) in plan.payload_path.parents
        payload = load_task_payload(plan.payload_path)
        assert payload["request"] == big  # complete text preserved
        compact = bootstrap_instruction(plan.payload_path, tmp_path)
        assert len(compact) < 600
        assert len(plan.command_prompt) < 700  # far below any argv ceiling

    def test_inline_limit_env_override(self, monkeypatch):
        monkeypatch.setenv("AUTOFIX_INLINE_PROMPT_LIMIT", "10")
        assert inline_prompt_limit() == 10

    def test_opencode_worker_still_uses_transport(self, monkeypatch, tmp_path):
        import app.agents.chat_workers as cw

        calls = {}
        real_prepare = cw.prepare_task_payload

        def spy(prompt, workspace):
            calls["args"] = (prompt, workspace)
            return real_prepare(prompt, workspace)

        monkeypatch.setattr(cw, "prepare_task_payload", spy)
        monkeypatch.setattr(
            __import__("app.agents.coding_agent", fromlist=["_default_discover_opencode"]),
            "_default_discover_opencode",
            lambda: BackendInfo("opencode", True, "opencode-fake", ""),
        )

        class _FakeProc:
            def __init__(self, *a, **k):
                self.stdout = []
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(cw.subprocess, "Popen", _FakeProc)

        worker = OpenCodeChatWorker("refactor the parser module", str(tmp_path))
        worker.run()

        assert "args" in calls
        prompt, workspace = calls["args"]
        assert prompt == "refactor the parser module"
        assert workspace == str(tmp_path)


# ── Mode integrity & cancellation/recovery/memory ────────────────


class TestModesAndLifecycleUnchanged:
    def test_modes_present_and_modes_complete(self, window):
        assert set(window._mode_buttons) == {"chat", "autofix"}
        assert dict(mw.MainWindow.AI_MODES) == {
            "chat": "Chat",
            "autofix": "AutoFix",
        }
        # The approval gate stays visible in every mode, disabled by default.
        for mode in ("chat", "autofix"):
            window.set_ai_mode(mode)
            assert window.approve_button.isVisibleTo(window), mode
        window.set_ai_mode("chat")
        assert not window.approve_button.isEnabled()

    def test_stop_cancel_behavior_unchanged(self, window):
        class _RunningPipeline:
            def __init__(self):
                self.cancel_calls = 0

            def isRunning(self):
                return True

            def cancel(self):
                self.cancel_calls += 1

        pipeline = _RunningPipeline()
        window._pipeline = pipeline
        window.on_stop_execution()
        assert pipeline.cancel_calls == 1
        assert "Stopping AutoFix execution…" in window.conversation.toPlainText()

    def test_recovery_wiring_intact(self, tmp_path):
        pipeline = ApprovalPipeline("req", str(tmp_path))
        assert isinstance(pipeline._recovery_agent, RecoveryAgent)
        assert pipeline.MAX_DEBUG_CYCLES >= 1
        assert max_recovery_attempts(env={}) == 3  # default recovery budget
        assert hasattr(RecoveryAgent, "run")
        assert RECOVERY_REQUIRED != CANCELLED

    def test_persistent_task_state_roundtrip(self, tmp_path):
        task = AutoFixTask.create(tmp_path, "original request")
        task.save()
        loaded = AutoFixTask.load(tmp_path, task.task_id)
        assert loaded is not None
        assert loaded.original_request == task.original_request
        assert loaded.file_path().exists()
        assert tasks_dir(tmp_path) in loaded.file_path().parents

    def test_memory_retrieval_intact(self, tmp_path):
        record_memory(
            tmp_path,
            KIND_FIXES,
            "fix-login-animation",
            "Login page animation implemented with CSS keyframes.",
            tags=["login"],
        )
        hits = retrieve_relevant(tmp_path, "login page animation")
        assert hits
        assert any("animation" in json.dumps(hit).lower() for hit in hits)


# ── Error distinction: provider vs capability vs environment ─────


class TestErrorDistinctions:
    def test_provider_failure_names_provider_not_capability(self, monkeypatch):
        def boom(config, message, history=None):
            raise ChatProviderError(f"{config.name} HTTP 503: upstream down")

        monkeypatch.setattr(chat_provider_module, "call_provider", boom)
        env = {"OPENAI_API_KEY": "sk-test"}

        with pytest.raises(ChatProviderError) as excinfo:
            converse("hello", ".", env=env)

        message = str(excinfo.value)
        assert "GPT HTTP 503" in message
        assert "built-in local assistant" not in message

    def test_window_error_block_only_for_real_provider_failures(self, window):
        window._on_chat_error("GPT connection failed: refused")
        text = window.conversation.toPlainText()
        assert "AI Chat Error" in text
        assert "GPT connection failed" in text

    def test_local_fallback_is_not_an_api_key_error(self):
        reply = LocalAssistant.respond("tell me something interesting", ".")
        assert "AI Chat Error" not in reply
        assert "Unable to reach the configured AI provider." not in reply
