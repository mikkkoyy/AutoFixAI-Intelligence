"""Workspace propagation tests: File → Open Folder must update everything."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.ui import main_window as mw


def test_set_active_workspace_string_compat(window):
    """Required compatibility contract:

        main_window.set_active_workspace(test_folder)
        assert main_window.active_workspace == test_folder
    """
    test_folder = "test_folder"
    window.set_active_workspace(test_folder)
    assert window.active_workspace == test_folder


def test_set_active_workspace_returns_true_for_valid(window, tmp_path):
    assert window.set_active_workspace(str(tmp_path)) is True


def test_set_active_workspace_rejects_empty(window):
    before = window.active_workspace
    assert window.set_active_workspace("") is False
    assert window.set_active_workspace(None) is False
    assert window.active_workspace == before


def test_active_workspace_and_project_path(window, tmp_path):
    window.set_active_workspace(str(tmp_path))
    assert window.active_workspace == str(tmp_path)
    assert window.project_path == str(tmp_path)
    assert window.agent_workspace == str(tmp_path)


def test_open_folder_dialog_sets_workspace(window, monkeypatch, tmp_path):
    monkeypatch.setattr(
        mw.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *args, **kwargs: str(tmp_path)),
    )
    window.open_project_folder()
    assert window.active_workspace == str(tmp_path)
    assert window.project_path == str(tmp_path)


def test_open_folder_dialog_cancel_keeps_workspace(window, monkeypatch, tmp_path):
    window.set_active_workspace(str(tmp_path))
    monkeypatch.setattr(
        mw.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *args, **kwargs: ""),
    )
    window.open_project_folder()
    assert window.active_workspace == str(tmp_path)


def test_explorer_refreshes_on_workspace_change(window, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")

    window.set_active_workspace(str(tmp_path))

    assert window.tree.topLevelItemCount() == 1
    root_item = window.tree.topLevelItem(0)
    assert root_item.text(0) == tmp_path.name
    child_names = {
        root_item.child(i).text(0) for i in range(root_item.childCount())
    }
    assert {"app", "tests", "README.md"} <= child_names


def test_explorer_expands_and_opens_files(window, tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    demo = sub / "module.py"
    demo.write_text("value = 1\n", encoding="utf-8")

    window.set_active_workspace(str(tmp_path))
    root_item = window.tree.topLevelItem(0)

    pkg_item = None
    for i in range(root_item.childCount()):
        if root_item.child(i).text(0) == "pkg":
            pkg_item = root_item.child(i)
    assert pkg_item is not None

    # Directory toggle expands/collapses without opening an editor tab.
    tabs_before = window.tabs.count()
    window.open_explorer_item(pkg_item)
    assert window.tabs.count() == tabs_before

    # Opening the file loads it into the existing editor tabs.
    file_item = pkg_item.child(0)
    window.open_explorer_item(file_item)
    assert window.tabs.count() == tabs_before + 1
    editor = window.tabs.currentWidget()
    assert "value = 1" in editor.toPlainText()


def test_chat_receives_workspace_context(window, tmp_path, monkeypatch):
    from app.agents.pipeline import PlanWorker

    window.set_active_workspace(str(tmp_path))
    assert window.chat_workspace_label.text() == tmp_path.name

    started = {}
    monkeypatch.setattr(
        PlanWorker, "start", lambda self: started.setdefault("ws", self._workspace)
    )
    window.set_ai_mode("autofix")
    window.on_chat_send("please fix the tests")
    assert started.get("ws") == str(tmp_path)


def test_chat_mode_worker_receives_workspace(window, tmp_path):
    from app.agents.chat_workers import ChatWorker

    window.set_active_workspace(str(tmp_path))

    started = {}
    # Capture the workspace handed to the conversational worker without
    # starting a real thread.
    original_start = ChatWorker.start
    ChatWorker.start = lambda self: started.setdefault("ws", self._workspace)
    try:
        window.on_chat_send("what files are in this project?")
    finally:
        ChatWorker.start = original_start

    assert started.get("ws") == str(tmp_path)


def test_agent_panel_receives_workspace(window, tmp_path):
    window.set_active_workspace(str(tmp_path))
    assert window.agent_workspace == str(tmp_path)


def test_window_title_follows_workspace(window, tmp_path):
    window.set_active_workspace(str(tmp_path))
    assert tmp_path.name in window.windowTitle()
