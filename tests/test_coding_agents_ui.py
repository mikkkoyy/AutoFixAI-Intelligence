"""Coding-agent status panel, Open Folder shortcut and workspace propagation."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pathlib import Path

from PySide6.QtGui import QKeySequence

from app.agents.coding_agent import BackendInfo
from app.agents.pipeline import ApprovalPipeline
from app.ui import main_window as mw


def _info(name, available, detail=""):
    return BackendInfo(name, available, f"C:/fake/{name}.cmd" if available else None, detail)


# ── CODING AGENTS indicators ───────────────────────────────────


class TestAgentStatusPanel:
    def test_panel_has_all_four_backend_labels(self, window):
        assert set(window.coding_agent_labels) == {
            "opencode", "openhands", "continue", "aider"
        }

    def test_available_backend_is_green(self, window):
        window.update_coding_agent_statuses(
            {"opencode": _info("opencode", True, "version 1.18.19")}
        )
        label = window.coding_agent_labels["opencode"]
        assert "AVAILABLE" in label.text()
        assert mw.MainWindow._STATUS_GREEN in label.text()

    def test_unavailable_backend_is_red(self, window):
        window.update_coding_agent_statuses({"aider": _info("aider", False)})
        label = window.coding_agent_labels["aider"]
        assert "UNAVAILABLE" in label.text()
        assert mw.MainWindow._STATUS_RED in label.text()

    def test_only_the_circle_is_colored(self, window):
        """Agent name and state must be OUTSIDE the colored span."""
        window.update_coding_agent_statuses(
            {
                "opencode": _info("opencode", True),
                "openhands": _info("openhands", False),
            }
        )
        green_row = window.coding_agent_labels["opencode"].text()
        red_row = window.coding_agent_labels["openhands"].text()

        # Colored portion contains only the circle character.
        assert f"<span style='color:{mw.MainWindow._STATUS_GREEN};'>&#9679;</span>" in green_row
        assert f"<span style='color:{mw.MainWindow._STATUS_RED};'>&#9679;</span>" in red_row

        # Name and state are plain text after the span.
        assert "> OpenCode&nbsp;&nbsp;AVAILABLE" in green_row
        assert "> OpenHands&nbsp;&nbsp;UNAVAILABLE" in red_row
        assert "color" not in green_row.split("</span>", 1)[1]

    def test_no_background_or_border_colors(self, window):
        window.update_coding_agent_statuses(
            {name: _info(name, True) for name in
             ("opencode", "openhands", "continue", "aider")}
        )
        for label in window.coding_agent_labels.values():
            assert "background" not in label.text()
            assert "border" not in label.text()
            assert label.styleSheet() == ""

    def test_primary_is_first_available(self, window):
        window.update_coding_agent_statuses(
            {
                "opencode": _info("opencode", False),
                "openhands": _info("openhands", True),
                "continue": _info("continue", True),
            }
        )
        assert "OpenHands" in window.primary_agent_label.text()
        assert window.primary_agent_label.styleSheet() == ""

    def test_no_agents_shows_honest_message(self, window):
        window.update_coding_agent_statuses(
            {name: _info(name, False) for name in
             ("opencode", "openhands", "continue", "aider")}
        )
        assert "NO CODING AGENT AVAILABLE" in window.primary_agent_label.text()

    def test_partial_result_does_not_crash(self, window):
        # Missing entries are treated as unavailable, not as errors.
        window.update_coding_agent_statuses({})
        assert "NO CODING AGENT" in window.primary_agent_label.text()

    def test_version_detail_shown_for_available(self, window):
        window.update_coding_agent_statuses(
            {"opencode": _info("opencode", True, "version 1.18.19")}
        )
        assert "version 1.18.19" in window.coding_agent_labels["opencode"].text()

    def test_unavailable_has_no_detail(self, window):
        window.update_coding_agent_statuses(
            {"aider": _info("aider", False, "some detail")}
        )
        assert "some detail" not in window.coding_agent_labels["aider"].text()


# ── AI CHAT AGENTS indicators ──────────────────────────────────


class TestChatAgentsPanel:
    def test_panel_has_gpt_claude_deepseek(self, window):
        assert set(window.chat_agent_labels) == {"GPT", "Claude", "DeepSeek"}

    def test_available_chat_agent_green_circle_only(self, window):
        from app.agents.chat_agents import ChatAgentInfo

        window.update_chat_agent_statuses(
            {"GPT": ChatAgentInfo("GPT", True, "API key configured")}
        )
        label = window.chat_agent_labels["GPT"]
        assert mw.MainWindow._STATUS_GREEN in label.text()
        assert "AVAILABLE" in label.text()
        assert label.styleSheet() == ""
        assert "&#9679;</span> GPT" in label.text()

    def test_unavailable_chat_agent_red_circle_only(self, window):
        from app.agents.chat_agents import ChatAgentInfo

        window.update_chat_agent_statuses(
            {"DeepSeek": ChatAgentInfo("DeepSeek", False)}
        )
        label = window.chat_agent_labels["DeepSeek"]
        assert mw.MainWindow._STATUS_RED in label.text()
        assert "UNAVAILABLE" in label.text()


# ── Diagnostic status ──────────────────────────────────────────


class TestDiagnosticStatus:
    def test_diagnostic_area_exists_in_right_panel(self, window):
        right = window.main_split.widget(2)
        assert right.isAncestorOf(window.diagnostics)
        assert right.isAncestorOf(window.diagnostic_status_label)

    def test_initial_status_is_waiting(self, window):
        assert window.diagnostic_status_label.text() == "STATUS: WAITING"

    def test_valid_states_accepted(self, window):
        for state in (
            "ANALYZING", "PLANNING", "APPROVED", "QUEUED", "CODING",
            "TESTING", "DEBUGGING", "REVIEWING", "VERIFYING",
            "COMPLETED", "FAILED",
        ):
            window.set_diagnostic_status(state)
            assert window.diagnostic_status_label.text() == f"STATUS: {state}"

    def test_invalid_state_ignored(self, window):
        window.set_diagnostic_status("WAITING")
        window.set_diagnostic_status("MADE_UP_STATE")
        assert window.diagnostic_status_label.text() == "STATUS: WAITING"

    def test_stage_start_maps_to_state(self, window):
        window._on_stage_started("Coding")
        assert window.diagnostic_status_label.text() == "STATUS: CODING"
        window._on_stage_started("Verification")
        assert window.diagnostic_status_label.text() == "STATUS: VERIFYING"

    def test_pipeline_finish_sets_completed_or_failed(self, window):
        window._on_pipeline_finished(True, "Execution PASSED")
        assert window.diagnostic_status_label.text() == "STATUS: COMPLETED"
        window._on_pipeline_finished(False, "Execution FAILED.")
        assert window.diagnostic_status_label.text() == "STATUS: FAILED"

    def test_approval_sets_approved_and_logs_activity(self, window, monkeypatch):
        monkeypatch.setattr(ApprovalPipeline, "start", lambda self: None)
        window._last_request = "task"
        window._on_plan_ready("1. Do A")

        window.on_approve_plan()

        assert window.diagnostic_status_label.text() == "STATUS: APPROVED"
        log = window.diagnostics.toPlainText()
        assert "Plan approved" in log
        assert "Creating job queue..." in log


# ── File → Open Folder ─────────────────────────────────────────


class TestOpenFolder:
    def test_shortcut_registered(self, window):
        seq = window.open_folder_shortcut.key()
        assert seq.toString().lower() == "ctrl+k, ctrl+o"

    def test_menu_action_uses_file_dialog(self, window, tmp_path, monkeypatch):
        chosen = []

        def fake_dialog(*args, **kwargs):
            chosen.append(1)
            return str(tmp_path / "chosen_project")

        monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", fake_dialog)

        (tmp_path / "chosen_project").mkdir()
        window.open_project_folder()

        assert chosen == [1]
        assert window.active_workspace == str(tmp_path / "chosen_project")

    def test_cancelled_dialog_keeps_workspace(self, window, monkeypatch):
        monkeypatch.setattr(
            mw.QFileDialog, "getExistingDirectory", lambda *a, **k: ""
        )
        before = window.active_workspace
        window.open_project_folder()
        assert window.active_workspace == before

    def test_open_folder_propagates_everywhere(self, qapp, window, tmp_path, monkeypatch):
        target = tmp_path / "new_ws"
        (target / "src").mkdir(parents=True)
        (target / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")

        monkeypatch.setattr(
            mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(target)
        )
        window.open_project_folder()

        raw = str(target)
        assert window.active_workspace == raw
        assert window.project_path == raw
        assert window.agent_workspace == raw

        # Explorer root refreshed to the new folder.
        top = window.tree.topLevelItem(0)
        assert top is not None
        assert top.text(0) == "new_ws"

        # Chat + title reflect the workspace.
        assert window.chat_workspace_label.text() == "new_ws"
        assert "new_ws" in window.windowTitle()

        # Terminal launcher resolves to the new workspace.
        assert window._workspace_path() == target

    def test_terminal_launches_in_new_workspace(self, window, tmp_path, monkeypatch):
        class FakePopen:
            last = None

            def __init__(self, cmd, cwd=None, creationflags=0, **kwargs):
                FakePopen.last = self
                self.cwd = cwd

        target = tmp_path / "term_ws"
        target.mkdir()

        monkeypatch.setattr(
            mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(target)
        )
        window.open_project_folder()
        monkeypatch.setattr(mw.subprocess, "Popen", FakePopen)
        window.launch_external_terminal("pwsh")

        assert FakePopen.last.cwd == str(target)

    def test_pipeline_receives_new_workspace(self, window, tmp_path, monkeypatch):
        from app.agents.pipeline import ApprovalPipeline

        target = tmp_path / "pipe_ws"
        target.mkdir()
        monkeypatch.setattr(
            mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(target)
        )
        window.open_project_folder()

        # The pipeline is constructed from the live workspace at approval time.
        pipeline = ApprovalPipeline("task", str(window._workspace_path()))
        assert pipeline._workspace == str(target)

    def test_menu_action_trigger_updates_workspace(self, qapp, window, tmp_path, monkeypatch):
        """Drive the REAL File → Open Folder QAction through trigger()."""
        target = tmp_path / "via_menu"
        target.mkdir()

        calls = {}

        def fake_dialog(parent, caption, directory):
            calls["parent"] = parent
            calls["caption"] = caption
            calls["directory"] = directory
            return str(target)

        monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", fake_dialog)

        file_menu = window.menuBar().actions()[0].menu()
        action = next(a for a in file_menu.actions() if a.text() == "Open Folder...")
        action.trigger()

        assert calls["parent"] is window, "dialog parent must be MainWindow"
        assert isinstance(calls["directory"], str), "start dir must be str (Open File pattern)"
        assert window.active_workspace == str(target)

    def test_shortcut_activates_same_handler(self, qapp, window, tmp_path, monkeypatch):
        """The Ctrl+K, Ctrl+O chord must reach open_project_folder too."""
        from PySide6.QtGui import QKeySequence
        from PySide6.QtTest import QTest

        target = tmp_path / "via_shortcut"
        target.mkdir()

        calls = []
        monkeypatch.setattr(
            mw.QFileDialog,
            "getExistingDirectory",
            lambda *a, **k: calls.append(1) or str(target),
        )

        window.show()
        qapp.processEvents()
        window.activateWindow()
        window.setFocus()
        qapp.processEvents()

        QTest.keySequence(window, QKeySequence("Ctrl+K, Ctrl+O"))
        qapp.processEvents()

        assert calls == [1], "shortcut must invoke the Open Folder dialog flow"
        assert window.active_workspace == str(target)


# ── Shortcut context ───────────────────────────────────────────


class TestShortcutWiring:
    def test_open_folder_shortcut_window_context(self, window):
        assert (
            window.open_folder_shortcut.context()
            == __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.ShortcutContext.WindowShortcut
        )

    def test_save_shortcut_still_present(self, window):
        assert isinstance(window.save_shortcut.key(), QKeySequence)
