"""Regression tests for the locked AutoFix-only intake flow.

Large or bulk-style multi-line input is accepted directly into the existing
AutoFix planning flow; it never creates a separate execution pipeline.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.ui.main_window as mw
from app.agents.pipeline import PlanWorker
from app.agents.chat_workers import OpenCodeChatWorker


def test_bulk_routes_to_plan_worker(window, monkeypatch, tmp_path):
    """Large/bulk-style intake must construct a PlanWorker with the exact
    request and workspace (i.e., route directly into AutoFix)."""

    captured = {}
    real_init = PlanWorker.__init__

    def fake_init(self, description, workspace, parent=None):
        # call the real initializer so the test environment remains consistent
        real_init(self, description, workspace, parent)
        captured["description"] = description
        captured["workspace"] = workspace

    monkeypatch.setattr(PlanWorker, "__init__", fake_init)

    window.set_active_workspace(str(tmp_path))
    window.set_ai_mode("autofix")
    request = "Analyze workspace and propose changes"
    window.on_chat_send(request)

    assert captured.get("description") == request
    assert isinstance(window._plan_worker, PlanWorker)
    assert "Analyzing the workspace" in window.conversation.toPlainText()


def test_large_paste_preserved_by_bulk(window, monkeypatch, tmp_path):
    """A large pasted source block must be accepted intact and handed to the
    AutoFix planner without truncation."""

    captured = {}
    real_init = PlanWorker.__init__

    def spy_init(self, description, workspace, parent=None):
        real_init(self, description, workspace, parent)
        captured["description"] = description

    monkeypatch.setattr(PlanWorker, "__init__", spy_init)

    window.set_active_workspace(str(tmp_path))
    window.set_ai_mode("autofix")

    # Construct a large paste (thousands of chars)
    body = "\n".join(f"def helper_{i}():\n    return {i}" for i in range(1500))
    request = f"Here is a large file:\n\n{body}\n\nPlease analyze and fix."

    window.on_chat_send(request)

    # The last request stored must match byte-for-byte
    assert window._last_request == request
    # The PlanWorker must receive the full request description
    assert captured.get("description") == request


def test_bulk_does_not_invoke_opencode(window, monkeypatch, tmp_path):
    """Bulk-style intake must not route to OpenCode automatically. OpenCode
    remains a separate internal worker when explicitly invoked."""

    # Replace OpenCodeChatWorker with a spy that records starts
    class FakeOpenCode:
        last = None
        def __init__(self, prompt, workspace, parent=None):
            FakeOpenCode.last = self
            self.prompt = prompt
            self.workspace = workspace
            self.started = False
        def isRunning(self):
            return False
        def cancel(self):
            pass
        def start(self):
            self.started = True

    monkeypatch.setattr(mw, "OpenCodeChatWorker", FakeOpenCode)

    window.set_ai_mode("autofix")
    window.on_chat_send("bulk request that might look like opencode")

    # OpenCode must NOT have been started automatically
    assert FakeOpenCode.last is None

    # Now explicitly invoke OpenCode via the internal handler and ensure it IS invoked
    window.set_active_workspace(str(tmp_path))
    window._handle_opencode_message("run opencode now")

    assert isinstance(FakeOpenCode.last, FakeOpenCode)
    assert FakeOpenCode.last.prompt == "run opencode now"
    assert FakeOpenCode.last.started is True


def test_approve_button_remains_disabled_until_plan_ready(window):
    """APPROVE & EXECUTE must remain visible but disabled until a plan
    is produced (approval gate). Bulk must not bypass approval."""

    window.set_ai_mode("autofix")
    window.on_chat_send("some bulk request")

    # Immediately after submission, pending plan is not yet present
    assert window.approve_button.isVisibleTo(window)
    assert not window.approve_button.isEnabled()

    # Simulate plan ready (PlanWorker would emit this) and check enabling
    window._last_request = "some bulk request"
    window._on_plan_ready("1. Do X\n2. Do Y")

    assert window._pending_plan is not None
    assert window.approve_button.isEnabled()


# ── Compression (20-line single-word labels) ─────────────────────


def test_under_20_lines_not_compressed(window):
    body = "\n".join(f"line {i}" for i in range(19))
    request = f"Here is a request:\n\n{body}\n\nPlease handle."
    window.set_ai_mode("autofix")
    window.on_chat_send(request)
    assert window._short_label is None


def test_exactly_20_lines_compressed(window):
    body = "\n".join(f"line {i}" for i in range(20))
    request = f"Spec:\n\n{body}\n\nDo it."
    window.set_ai_mode("autofix")
    window.on_chat_send(request)
    assert window._short_label is not None
    assert isinstance(window._short_label, str)
    assert " " not in window._short_label
    assert window._last_request == request


def test_over_20_lines_compressed_and_single_word(window):
    body = "\n".join(f"def f{i}(): pass" for i in range(25))
    request = f"Here:\n\n{body}\n\nAnalyze"
    window.set_ai_mode("autofix")
    window.on_chat_send(request)
    label = window._short_label
    assert label is not None
    assert label.upper() == label
    assert " " not in label


def test_single_word_label_format(window):
    body = "\n".join(f"x{i}" for i in range(30))
    request = f"Big:\n\n{body}\n\nPlease."
    window.set_ai_mode("autofix")
    window.on_chat_send(request)
    label = window._short_label
    assert label is not None
    assert label != ""
    import re
    assert re.match(r"^[A-Z0-9_]+$", label)


def test_original_request_preserved_for_compressed_task(window, monkeypatch, tmp_path):
    captured = {}
    real_init = PlanWorker.__init__

    def fake_init(self, description, workspace, parent=None):
        real_init(self, description, workspace, parent)
        captured["description"] = description

    monkeypatch.setattr(PlanWorker, "__init__", fake_init)

    body = "\n".join(f"line {i}" for i in range(22))
    request = f"Request:\n\n{body}\n\nDo it."
    window.set_active_workspace(str(tmp_path))
    window.set_ai_mode("autofix")
    window.on_chat_send(request)

    # PlanWorker must receive the full original request
    assert captured.get("description") == request
    # The UI retains the original request
    assert window._last_request == request


def test_bulk_does_not_compress_chat_mode(window):
    body = "\n".join(f"line {i}" for i in range(25))
    request = f"Request:\n\n{body}\n\nDo it."
    window.set_ai_mode("chat")
    window.on_chat_send(request)
    assert window._short_label is None
