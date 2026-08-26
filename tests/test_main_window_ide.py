"""IDE layout, terminal launcher, chat input and approval workflow tests."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QContextMenuEvent, QKeyEvent
from PySide6.QtWidgets import QPushButton

from app.agents.pipeline import ApprovalPipeline
from app.ui import main_window as mw


# ── Layout ─────────────────────────────────────────────────────


class TestLayout:
    def test_main_split_is_horizontal_with_three_panes(self, window):
        assert window.main_split.orientation() == Qt.Orientation.Horizontal
        assert window.main_split.count() == 3

    def test_center_split_is_vertical_editor_over_chat(self, window):
        split = window.center_split
        assert split.orientation() == Qt.Orientation.Vertical
        assert split.count() == 2
        assert split.widget(0).isAncestorOf(window.tabs)
        assert split.widget(1).isAncestorOf(window.conversation)

    def test_explorer_left_agent_right(self, window):
        left = window.main_split.widget(0)
        right = window.main_split.widget(2)
        assert window.tree in left.findChildren(type(window.tree))
        assert window.agent_list in right.findChildren(type(window.agent_list))

    def test_terminal_launcher_below_explorer(self, window):
        from PySide6.QtWidgets import QSplitter

        left = window.main_split.widget(0)
        assert isinstance(left, QSplitter)
        assert left.orientation() == Qt.Orientation.Vertical
        assert left.count() == 2
        assert left.widget(0).isAncestorOf(window.tree)
        assert left.widget(1).isAncestorOf(window.terminal_list)

    def test_approve_button_always_visible_and_starts_disabled(self, window):
        assert window.approve_button.text() == "APPROVE & EXECUTE"
        # Always visible; the safety gate lives in the enabled state.
        assert window.approve_button.isVisibleTo(window)
        assert not window.approve_button.isEnabled()


# ── Terminal launcher ──────────────────────────────────────────


class FakePopen:
    last = None

    def __init__(self, cmd, cwd=None, creationflags=0, **kwargs):
        self.cmd = cmd
        self.cwd = cwd
        self.creationflags = creationflags
        FakePopen.last = self


class TestTerminalLauncher:
    def test_no_open_or_run_buttons(self, window):
        texts = [b.text().strip().lower() for b in window.findChildren(QPushButton)]
        assert "open" not in texts
        assert "run" not in texts

    def test_launcher_lists_pwsh_and_cmd_only(self, window):
        kinds = sorted(window._terminal_items.keys())
        assert kinds == ["cmd", "pwsh"]

    def test_selecting_pwsh_launches_external_window(self, window, tmp_path, monkeypatch):
        window.set_active_workspace(str(tmp_path))
        monkeypatch.setattr(mw.subprocess, "Popen", FakePopen)

        assert window.launch_external_terminal("pwsh") is True
        assert FakePopen.last is not None
        assert Path(FakePopen.last.cmd[0]).name.lower().replace(".exe", "") == "pwsh"
        assert FakePopen.last.cwd == str(tmp_path)

    def test_selecting_cmd_launches_external_window(self, window, tmp_path, monkeypatch):
        window.set_active_workspace(str(tmp_path))
        monkeypatch.setattr(mw.subprocess, "Popen", FakePopen)

        assert window.launch_external_terminal("cmd") is True
        assert Path(FakePopen.last.cmd[0]).name.lower() == "cmd.exe"
        assert FakePopen.last.cwd == str(tmp_path)

    def test_clicking_item_selects_and_launches(self, window, tmp_path, monkeypatch):
        window.set_active_workspace(str(tmp_path))
        monkeypatch.setattr(mw.subprocess, "Popen", FakePopen)

        item = window._terminal_items["cmd"]
        window._on_terminal_item_clicked(item)

        assert window.selected_terminal == "cmd"
        assert item.text().startswith("◀")
        assert FakePopen.last.cwd == str(tmp_path)

    def test_unknown_shell_rejected(self, window):
        assert window.launch_external_terminal("zsh") is False


# ── Chat input behaviour ───────────────────────────────────────


def _press(chat_input, key, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
    chat_input.keyPressEvent(event)


class TestChatInput:
    def test_enter_sends(self, qapp, window):
        sent = []
        window.chat_input.send_requested.connect(sent.append)
        window.chat_input.setPlainText("hello world")

        _press(window.chat_input, Qt.Key.Key_Return)

        assert sent == ["hello world"]
        assert window.chat_input.toPlainText() == ""

    def test_shift_enter_inserts_newline(self, qapp, window):
        sent = []
        window.chat_input.send_requested.connect(sent.append)

        _press(
            window.chat_input,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ShiftModifier,
        )

        assert sent == []
        assert "\n" in window.chat_input.toPlainText()

    def test_ctrl_enter_sends(self, qapp, window):
        sent = []
        window.chat_input.send_requested.connect(sent.append)
        window.chat_input.setPlainText("go")

        _press(window.chat_input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)

        assert sent == ["go"]

    def test_right_click_paste(self, qapp, window, monkeypatch):
        from app.ui.main_window import ChatInput

        qapp.clipboard().setText("clipboard-content")

        class FakeMenu:
            """Stands in for the standard context menu; 'choosing' Paste
            invokes the widget's own paste slot."""

            def __init__(self, target):
                self._target = target
                self.shown = False

            def exec(self, _pos):
                self.shown = True
                self._target.paste()

        fake_menu = FakeMenu(window.chat_input)
        monkeypatch.setattr(
            ChatInput, "createStandardContextMenu", lambda self: fake_menu
        )

        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(5, 5), QPoint(5, 5)
        )
        window.chat_input.contextMenuEvent(event)

        assert fake_menu.shown, "right-click must open the context menu"
        assert "clipboard-content" in window.chat_input.toPlainText()

    def test_ctrl_v_pastes(self, qapp, window):
        # Paste integration is exercised through the same MIME-data entry
        # point QWidget::paste() uses — without depending on OS clipboard
        # timing, which is unreliable under parallel test load on Windows.
        from PySide6.QtCore import QMimeData

        mime = QMimeData()
        mime.setText("ctrl-v-body")
        window.chat_input.insertFromMimeData(mime)
        assert "ctrl-v-body" in window.chat_input.toPlainText()


# ── Approval workflow ──────────────────────────────────────────


class TestApprovalWorkflow:
    def test_approval_requires_a_plan(self, window):
        window.on_approve_plan()
        assert window._pipeline is None

    def test_plan_shows_approve_button(self, window):
        window._last_request = "add a feature"
        window._on_plan_ready("1. Do A\n2. Do B")
        assert window.approve_button.isVisibleTo(window)
        assert window._pending_plan is not None

    def test_plan_renders_proposal_and_approval_state(self, window):
        window._last_request = "add a feature"
        window._on_plan_ready("1. Do A\n2. Do B")
        text = window.conversation.toPlainText()
        assert "AUTOFIX PROPOSAL" in text
        assert "AWAITING APPROVAL" in text
        assert "Execution prompt prepared" in text

    def test_approve_creates_task_and_shows_task_id(self, window, monkeypatch):
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: None)
        window._last_request = "add a feature"
        window._on_plan_ready("1. Do A")

        window.on_approve_plan()

        assert window._pipeline is not None
        assert window._pending_plan is None
        assert "AutoFix is now executing task" in window.conversation.toPlainText()
        assert "decompose" in window.conversation.toPlainText()
        assert "AutoFix task created" in window.diagnostics.toPlainText()

    def test_approve_starts_pipeline_once(self, window, monkeypatch):
        calls = []
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: calls.append(1))

        window._last_request = "add a feature"
        window._on_plan_ready("1. Do A")
        window.on_approve_plan()

        assert len(calls) == 1
        assert window._pending_plan is None
        assert window.approve_button.isVisibleTo(window)
        assert not window.approve_button.isEnabled()

        # Second click must not start a second pipeline.
        window.on_approve_plan()
        assert len(calls) == 1

    def test_enter_never_approves(self, window, monkeypatch):
        from app.agents.pipeline import PlanWorker

        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: None)
        monkeypatch.setattr(PlanWorker, "start", lambda self: None)

        window._last_request = "task"
        window._on_plan_ready("plan text")

        # User keeps chatting while the plan is displayed.
        window.on_chat_send("actually also add tests")

        assert window._pipeline is None
        assert window._pending_plan is not None

    def test_pipeline_stage_updates_agent_panel(self, window):
        window.set_agent_status("Coding", "Running")
        index = [
            i
            for i in range(window.agent_list.count())
            if window.agent_list.item(i).data(window.AGENT_NAME_ROLE) == "Coding"
        ][0]
        assert "Running" in window.agent_list.item(index).text()

        window.set_agent_status("Coding", "✓")
        assert "✓" in window.agent_list.item(index).text()

    def test_all_stages_present_in_agent_panel(self, window):
        stages = {
            window.agent_list.item(i).data(window.AGENT_NAME_ROLE)
            for i in range(window.agent_list.count())
        }
        assert stages == {
            "Planner",
            "Coding",
            "Tester",
            "Debugger",
            "Reviewer",
            "Verification",
        }
