"""Coding agent priority tests: OpenCode → OpenHands → Continue → Aider."""

import sys
import os
import io

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agents.coding_agent import BackendInfo, CodingAgentRunner, PRIORITY_ORDER


class FakeProc:
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = io.StringIO(out)
        self.cmd = None
        self.cwd = None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def make_runner(
    op_available=True,
    aider_available=True,
    openhands_available=False,
    continue_available=False,
    rc=0,
    out="",
    capture=None,
    popen=None,
):
    def discover_opencode():
        if op_available:
            return BackendInfo("opencode", True, "C:/fake/opencode.cmd")
        return BackendInfo("opencode", False, None, "not found")

    def discover_openhands():
        if openhands_available:
            return BackendInfo("openhands", True, "C:/fake/openhands.cmd")
        return BackendInfo("openhands", False, None, "not found")

    def discover_continue():
        if continue_available:
            return BackendInfo("continue", True, "C:/fake/cn.cmd")
        return BackendInfo("continue", False, None, "not found")

    def discover_aider():
        if aider_available:
            return BackendInfo("aider", True, "C:/fake/aider.exe")
        return BackendInfo("aider", False, None, "not found")

    if popen is None:
        def popen(cmd, cwd=None, **kwargs):
            proc = FakeProc(rc, out)
            proc.cmd = cmd
            proc.cwd = cwd
            if capture is not None:
                capture["cmd"] = cmd
                capture["cwd"] = cwd
            return proc

    return CodingAgentRunner(
        discover_opencode=discover_opencode,
        discover_aider=discover_aider,
        popen=popen,
        discover_openhands=discover_openhands,
        discover_continue=discover_continue,
    )


class TestPriority:
    def test_opencode_is_priority_one(self, tmp_path):
        capture = {}
        runner = make_runner(capture=capture)
        result = runner.execute("fix the bug", tmp_path)

        assert result.backend == "opencode"
        assert result.success is True
        assert capture["cmd"][0] == "C:/fake/opencode.cmd"
        assert capture["cmd"][1] == "run"

    def test_aider_is_fallback(self, tmp_path):
        capture = {}
        runner = make_runner(op_available=False, capture=capture)
        result = runner.execute("fix the bug", tmp_path)

        assert result.backend == "aider"
        assert "--yes-always" in capture["cmd"]
        assert "--message" in capture["cmd"]

    def test_no_agent_reports_honest_failure(self, tmp_path):
        runner = make_runner(op_available=False, aider_available=False)
        result = runner.execute("fix the bug", tmp_path)

        assert result.backend is None
        assert result.success is False
        assert "No coding agent available" in result.error


class TestWorkspace:
    def test_opencode_runs_in_workspace(self, tmp_path):
        capture = {}
        runner = make_runner(capture=capture)
        runner.execute("task", tmp_path)
        assert capture["cwd"] == str(tmp_path)

    def test_aider_runs_in_workspace(self, tmp_path):
        capture = {}
        runner = make_runner(op_available=False, capture=capture)
        runner.execute("task", tmp_path)
        assert capture["cwd"] == str(tmp_path)


class TestAiderModelConfig:
    def test_configured_model_passed_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOFIX_AIDER_MODEL", "gpt-4o-mini")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        capture = {}
        runner = make_runner(op_available=False, capture=capture)
        runner.execute("task", tmp_path)

        cmd = capture["cmd"]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gpt-4o-mini"

    def test_openrouter_key_prefixes_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOFIX_AIDER_MODEL", "anthropic/claude-3.5-sonnet")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

        capture = {}
        runner = make_runner(op_available=False, capture=capture)
        runner.execute("task", tmp_path)

        cmd = capture["cmd"]
        assert cmd[cmd.index("--model") + 1] == "openrouter/anthropic/claude-3.5-sonnet"

    def test_bare_model_with_openrouter_gets_prefixed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTOFIX_AIDER_MODEL", "deepseek/deepseek-chat")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

        capture = {}
        runner = make_runner(op_available=False, capture=capture)
        runner.execute("task", tmp_path)

        cmd = capture["cmd"]
        assert cmd[cmd.index("--model") + 1].startswith("openrouter/")

    def test_no_model_hardcoded_when_unconfigured(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUTOFIX_AIDER_MODEL", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        capture = {}
        runner = make_runner(op_available=False, capture=capture)
        runner.execute("task", tmp_path)

        assert "--model" not in capture["cmd"]


class TestFailureDetection:
    def test_model_failure_detected(self, tmp_path):
        runner = make_runner(op_available=False, rc=1, out="Unknown model: free/x")
        result = runner.execute("task", tmp_path)

        assert result.success is False
        assert "model" in result.error.lower()

    def test_auth_failure_detected(self, tmp_path):
        runner = make_runner(rc=1, out="Error: API key not valid (401)")
        result = runner.execute("task", tmp_path)

        assert result.success is False
        assert "auth" in result.error.lower() or "api key" in result.error.lower()

    def test_generic_failure_includes_exit_code(self, tmp_path):
        runner = make_runner(rc=3, out="boom")
        result = runner.execute("task", tmp_path)

        assert result.success is False
        assert "3" in result.error

    def test_output_streamed_to_callback(self, tmp_path):
        lines = []
        runner = make_runner(out="line-one\nline-two\n")
        runner.execute("task", tmp_path, on_output=lines.append)

        assert "line-one" in lines
        assert "line-two" in lines


class TestDetectionChain:
    """OpenCode → OpenHands → Continue → Aider automatic priority."""

    def test_priority_order_constant(self):
        assert PRIORITY_ORDER == ("opencode", "openhands", "continue", "aider")

    def test_detect_backends_returns_all_four(self):
        backends = make_runner().detect_backends()
        assert set(backends) == {"opencode", "openhands", "continue", "aider"}

    def test_openhands_is_second(self, tmp_path):
        capture = {}
        runner = make_runner(
            op_available=False, openhands_available=True, capture=capture
        )
        result = runner.execute("task", tmp_path)

        assert result.backend == "openhands"
        # Documented headless invocation: openhands --headless -t "<task>"
        assert "--headless" in capture["cmd"]
        assert "-t" in capture["cmd"]

    def test_continue_is_third(self, tmp_path):
        capture = {}
        runner = make_runner(
            op_available=False,
            openhands_available=False,
            continue_available=True,
            capture=capture,
        )
        result = runner.execute("task", tmp_path)

        assert result.backend == "continue"
        # Documented headless invocation: cn -p "<prompt>"
        assert "-p" in capture["cmd"]

    def test_full_fallback_chain_order(self, tmp_path):
        seen = []

        def popen(cmd, cwd=None, **kwargs):
            seen.append(cmd[0])
            return FakeProc(rc=1, out="")  # starts but fails honestly

        runner = make_runner(
            op_available=True,
            openhands_available=True,
            continue_available=True,
            aider_available=True,
            popen=popen,
        )
        result = runner.execute("task", tmp_path)

        # A backend that STARTS but fails is reported honestly — no silent
        # cascade through the remaining backends.
        assert result.backend == "opencode"
        assert result.success is False
        assert len(seen) == 1

    def test_unstartable_backend_falls_through_to_next(self, tmp_path):
        started = []

        def popen(cmd, cwd=None, **kwargs):
            if "opencode" in str(cmd[0]).lower():
                raise OSError("cannot launch")
            started.append(cmd[0])
            return FakeProc(rc=0, out="ok")

        runner = make_runner(
            op_available=True,
            openhands_available=True,
            aider_available=True,
            popen=popen,
        )
        result = runner.execute("task", tmp_path)

        assert result.backend == "openhands"
        assert result.success is True
        assert len(started) == 1

    def test_no_agent_reports_honest_failure_with_all_names(self, tmp_path):
        runner = make_runner(
            op_available=False,
            openhands_available=False,
            continue_available=False,
            aider_available=False,
        )
        result = runner.execute("task", tmp_path)

        assert result.backend is None
        assert result.success is False
        assert "No coding agent available" in result.error

    def test_primary_backend_follows_priority(self):
        assert make_runner().primary_backend().name == "opencode"
        assert (
            make_runner(op_available=False, openhands_available=True)
            .primary_backend()
            .name
            == "openhands"
        )
        assert (
            make_runner(
                op_available=False,
                openhands_available=False,
                continue_available=True,
            )
            .primary_backend()
            .name
            == "continue"
        )
        assert (
            make_runner(
                op_available=False,
                openhands_available=False,
                continue_available=False,
            )
            .primary_backend()
            .name
            == "aider"
        )
        assert (
            make_runner(
                op_available=False,
                openhands_available=False,
                continue_available=False,
                aider_available=False,
            )
            .primary_backend()
            .name
            == "none"
        )


class TestDefaultDetectors:
    """Real detectors must never raise and must be honest about absence."""

    def test_default_detectors_return_backend_info(self):
        from app.agents import coding_agent as ca

        for detector in (
            ca._default_discover_opencode,
            ca._default_discover_openhands,
            ca._default_discover_continue,
            ca._default_discover_aider,
        ):
            info = detector()
            assert isinstance(info, BackendInfo)
            if not info.available:
                assert info.executable is None
                assert info.detail

    def test_openhands_absent_means_unavailable(self, monkeypatch):
        from app.agents import coding_agent as ca

        monkeypatch.setattr(ca.shutil, "which", lambda name: None)
        info = ca._default_discover_openhands()
        assert info.available is False

    def test_continue_config_dir_alone_is_not_usable(self, monkeypatch, tmp_path):
        from app.agents import coding_agent as ca

        monkeypatch.setattr(ca.shutil, "which", lambda name: None)
        monkeypatch.setattr(ca.Path, "home", lambda: tmp_path)
        (tmp_path / ".continue").mkdir()
        info = ca._default_discover_continue()

        # Config directory exists but no CLI — must NOT claim availability.
        assert info.available is False
        assert "no cn CLI" in info.detail
