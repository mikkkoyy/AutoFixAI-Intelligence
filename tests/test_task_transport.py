"""Test A — Large task transport.

A huge AutoFix request must NEVER travel as one giant command-line argument
(the previous ``opencode exited with code 1`` failure).  It must be persisted
in full under ``<workspace>\\.autofix\\tasks\\`` while OpenCode receives only
a compact bootstrap instruction pointing at the payload.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import io

from app.agents.coding_agent import BackendInfo, CodingAgentRunner
from app.agents.task_transport import (
    DEFAULT_INLINE_PROMPT_LIMIT,
    inline_prompt_limit,
    load_task_payload,
    prepare_task_payload,
)


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.stdout = io.StringIO("ok")
        self.cmd = None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _make_runner(capture):
    def discover_opencode():
        return BackendInfo("opencode", True, "C:/fake/opencode.cmd")

    def discover_none():
        return BackendInfo("x", False, None, "not found")

    def popen(cmd, cwd=None, **kwargs):
        proc = _FakeProc()
        proc.cmd = cmd
        capture["cmd"] = cmd
        capture["cwd"] = cwd
        return proc

    return CodingAgentRunner(
        discover_opencode=discover_opencode,
        discover_aider=discover_none,
        discover_openhands=discover_none,
        discover_continue=discover_none,
        popen=popen,
    )


def _large_request(lines: int = 900) -> str:
    return "\n".join(
        f"Requirement {i}: modify module_{i}.py so that function "
        f"`handle_{i}` validates its inputs and updates the registry. "
        "Error message on failure: E-{i}-invalid. Command: pytest -q tests/"
        for i in range(lines)
    )


class TestInlineVsTransport:
    def test_small_prompt_stays_inline(self, tmp_path):
        plan = prepare_task_payload("fix the bug", tmp_path)
        assert plan.transported is False
        assert plan.command_prompt == "fix the bug"
        assert plan.payload_path is None

    def test_default_limit_is_conservative(self):
        assert inline_prompt_limit({}) == DEFAULT_INLINE_PROMPT_LIMIT
        assert DEFAULT_INLINE_PROMPT_LIMIT < 32767 // 2

    def test_env_override_enables_transport_early(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOFIX_INLINE_PROMPT_LIMIT", "10")
        assert inline_prompt_limit() == 10
        plan = prepare_task_payload("short task but over ten chars", tmp_path)
        assert plan.transported is True

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AUTOFIX_INLINE_PROMPT_LIMIT", "-5")
        assert inline_prompt_limit() == DEFAULT_INLINE_PROMPT_LIMIT
        monkeypatch.setenv("AUTOFIX_INLINE_PROMPT_LIMIT", "abc")
        assert inline_prompt_limit() == DEFAULT_INLINE_PROMPT_LIMIT


class TestLargeTransport:
    def test_large_request_persisted_complete_and_bootstrap_compact(
        self, tmp_path
    ):
        request = _large_request()
        assert len(request) > 20000

        plan = prepare_task_payload(request, tmp_path)

        assert plan.transported is True
        # Compact bootstrap — nothing like the size of the request.
        assert len(plan.command_prompt) < 600
        # Complete request preserved byte-for-byte on disk.
        payload = load_task_payload(plan.payload_path)
        assert payload["request"] == request
        assert payload["kind"] == "autofix-task-payload"
        # Payload lives inside <workspace>\.autofix\tasks\
        assert ".autofix" in str(plan.payload_path)
        assert "tasks" in str(plan.payload_path)

    def test_bootstrap_references_relative_payload(self, tmp_path):
        plan = prepare_task_payload(_large_request(), tmp_path)
        assert ".autofix/tasks/" in plan.command_prompt
        assert plan.payload_path.name in plan.command_prompt
        assert "never truncate" in plan.command_prompt.lower()

    def test_nothing_is_truncated_even_for_huge_requests(self, tmp_path):
        request = _large_request(2500)  # ~60k chars
        plan = prepare_task_payload(request, tmp_path)
        payload = load_task_payload(plan.payload_path)
        assert payload["request"] == request
        assert len(payload["request"]) == len(request)

    def test_extra_context_lands_in_payload(self, tmp_path):
        plan = prepare_task_payload(
            _large_request(), tmp_path, extra_context={"approved_plan": "1. do it"}
        )
        payload = load_task_payload(plan.payload_path)
        assert payload["approved_plan"] == "1. do it"


class TestCodingAgentUsesTransport:
    def test_runner_never_puts_large_prompt_on_command_line(self, tmp_path):
        capture = {}
        runner = _make_runner(capture)
        request = _large_request()

        result = runner.execute(request, tmp_path)

        assert result.success is True
        cmd = capture["cmd"]
        # The argv prompt is the compact bootstrap, NOT the request.
        assert len("".join(cmd)) < 4000
        assert request[:200] not in "".join(cmd)
        assert "opencode" in str(cmd[0]).lower()
        assert cmd[1] == "run"
        # Full request persisted inside the workspace.
        transport = runner.last_transport
        assert transport.transported is True
        payload = load_task_payload(transport.payload_path)
        assert payload["request"] == request

    def test_runner_small_prompt_has_no_payload(self, tmp_path):
        capture = {}
        runner = _make_runner(capture)
        runner.execute("tiny fix", tmp_path)
        assert runner.last_transport.transported is False
        assert capture["cmd"][2] == "tiny fix"

    def test_payload_context_recorded_in_file(self, tmp_path):
        capture = {}
        runner = _make_runner(capture)
        runner.payload_context = {"approved_plan": "step one"}
        runner.execute(_large_request(), tmp_path)
        payload = load_task_payload(runner.last_transport.payload_path)
        assert payload["approved_plan"] == "step one"
