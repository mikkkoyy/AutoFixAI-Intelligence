from pathlib import Path
import os
import shutil
import subprocess
import uuid

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QFont,
    QTextCursor,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QMenu,
)

from app.agents.autofix_task import AutoFixTask
from app.agents.chat_workers import (
    ChatWorker,
    KnowledgeSaveWorker,
    OpenCodeChatWorker,
)
from app.agents.large_input import is_large_input, summarize_large_input
from app.agents.orchestrator import AgentOrchestrator
from app.agents.coding_agent import CodingAgentRunner
from app.agents.pipeline import ApprovalPipeline, PlanWorker
from app.agents.task_compressor import compress_task
from app.agents.task_memory import (
    describe_remaining,
    load_task_record,
    save_task_record,
    update_task_record,
)
from app.builder.project_builder import ProjectBuilder
from app.dependencies.checker import DependencyChecker
from app.verification.verifier import ProjectVerifier


STYLE = """
QWidget {
    background: #0f1117;
    color: #e6eaf0;
    font-family: "Segoe UI";
    font-size: 13px;
}
QMainWindow { background: #0f1117; }
QMenuBar, QMenu {
    background: #151922;
    color: #d9dee8;
    border: 0;
}
QMenuBar::item:selected, QMenu::item:selected { background: #252c3a; }
QToolButton { color: #d9dee8; padding: 6px 9px; }
QToolButton:hover { background: #252c3a; }
QPushButton {
    background: #202633;
    border: 1px solid #30394a;
    border-radius: 6px;
    padding: 7px 11px;
}
QPushButton:hover { background: #2a3343; }
QPushButton:disabled { color: #6b7484; }
QPushButton#Primary {
    background: #3b82f6;
    border: 0;
    font-weight: 600;
}
QPushButton#Primary:hover { background: #4b91ff; }
QPushButton#Approve {
    background: #1f7a4d;
    border: 0;
    font-weight: 700;
    padding: 10px 14px;
}
QPushButton#Approve:hover { background: #26995f; }
QPushButton#Danger { background: #52222a; border-color: #74313b; }
QPushButton#ModeButton {
    background: transparent;
    border: 1px solid #30394a;
    border-radius: 6px;
    padding: 3px 10px;
    color: #aeb7c6;
    font-size: 12px;
}
QPushButton#ModeButton:hover { background: #252c3a; }
QPushButton#ModeButton:checked {
    background: #263147;
    border-color: #3b82f6;
    color: #e6eaf0;
    font-weight: 600;
}
QPushButton#ExplorerRefresh {
    background: transparent;
    border: 0;
    padding: 3px 7px;
    font-size: 15px;
}
QPushButton#ExplorerRefresh:hover { background: #252c3a; }
QFrame#PanelHeader {
    background: #151922;
    border-bottom: 1px solid #262d3a;
}
QLabel#Brand { font-size: 18px; font-weight: 700; }
QLabel#Muted { color: #8993a5; }
QTreeWidget, QListWidget, QTextEdit, QLineEdit, QPlainTextEdit {
    background: #0b0e13;
    border: 1px solid #252c38;
    color: #e6eaf0;
}
QTreeWidget {
    border-left: 0;
    border-top: 0;
    border-bottom: 0;
}
QTreeWidget::item:selected, QListWidget::item:selected { background: #263147; }
QTreeWidget::item:hover, QListWidget::item:hover { background: #1b2230; }
QTabWidget::pane { border: 1px solid #252c38; }
QTabBar::tab {
    background: #151922;
    padding: 9px 16px;
    border-right: 1px solid #252c38;
}
QTabBar::tab:selected { background: #0b0e13; }
QTextEdit { padding: 10px; }
QPlainTextEdit { padding: 8px; }
QLineEdit {
    padding: 8px 10px;
    border-top: 1px solid #30394a;
    border-left: 0;
    border-right: 0;
    border-bottom: 0;
    border-radius: 0;
    font-family: "Consolas";
}
QStatusBar { background: #151922; color: #aeb7c6; }
QSplitter::handle { background: #202632; }
"""


class TerminalInput(QLineEdit):
    """Terminal-style input with an explicit Paste context menu."""

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        paste = menu.addAction("Paste")
        copy = menu.addAction("Copy")
        menu.addSeparator()
        clear = menu.addAction("Clear Input")

        selected = menu.exec(event.globalPos())

        if selected == paste:
            self.insert(QApplication.clipboard().text())
        elif selected == copy:
            self.copy()
        elif selected == clear:
            self.clear()


class ExplorerTree(QTreeWidget):
    """Explorer tree with an explicit Qt context menu."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.context_menu_requested = None

    def contextMenuEvent(self, event):
        if self.context_menu_requested:
            self.context_menu_requested(event)
            event.accept()
            return
        super().contextMenuEvent(event)


class ChatInput(QPlainTextEdit):
    """Multi-line chat input.

    Enter / Ctrl+Enter send the message, Shift+Enter inserts a newline.
    The standard context menu (including Paste) is preserved.
    """

    send_requested = Signal(str)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if shift:
                super().keyPressEvent(event)
            else:
                self._emit_send()
                event.accept()
            return

        if ctrl and shift and key == Qt.Key.Key_V:
            self.paste()
            event.accept()
            return

        super().keyPressEvent(event)

    def _emit_send(self):
        text = self.toPlainText().strip()
        if not text:
            return
        self.send_requested.emit(text)
        self.clear()

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPos())
        event.accept()


class MainWindow(QMainWindow):
    PATH_ROLE = Qt.ItemDataRole.UserRole
    DIR_ROLE = Qt.ItemDataRole.UserRole + 1
    AGENT_NAME_ROLE = Qt.ItemDataRole.UserRole + 2

    AGENT_STAGES = [
        "Planner",
        "Coding",
        "Tester",
        "Debugger",
        "Reviewer",
        "Verification",
    ]

    # Mode routing (deterministic, per send):
    #   Chat     → Chat AI provider only (never AutoFix/OpenCode/Bulk)
    #   AutoFix  → existing AutoFix pipeline (plan → approve → execute)
    #   Bulk     → AutoFix input mode: routed into the AutoFix pipeline
    #              automatically — no manual transfer, no confirmation
    #   OpenCode → explicit OpenCode CLI workflow only
    AI_MODES = [
        ("chat", "Chat"),
        ("autofix", "AutoFix"),
    ]

    def __init__(self):
        super().__init__()

        self.setWindowTitle("AutoFix AI Studio")
        self.resize(1450, 900)
        self.setMinimumSize(1100, 700)

        root = self.project_root()
        self.active_workspace = str(root)
        self.project_path = str(root)
        self.agent_workspace = str(root)

        self.selected_terminal = "pwsh"
        self._pending_plan = None
        self._last_request = ""
        self._plan_worker = None
        self._pipeline = None
        self._backend_worker = None
        #: Structured proposal currently awaiting approval in Chat (dict).
        self._active_chat_proposal = None
        #: Dedupe key set for per-task worker notifications (auth/config).
        self._worker_notify_seen: set[str] = set()
        #: Shared-knowledge notification state (non-blocking card).
        self._knowledge_card = None
        self._knowledge_save_worker = None
        self._knowledge_payload: dict | None = None

        self.current_ai_mode = "chat"
        self._mode_buttons = {}
        self._mode_group = None
        self._chat_worker_thread = None
        self._conversation_history: list[tuple[str, str]] = []
        self._active_task_memory: Path | None = None
        # Short UI label generated for large Bulk submissions (20+ lines).
        # Set by _handle_bulk_message and included in the pending plan.
        self._short_label: str | None = None
        #: Tool-event bridge for the ChatGPT tool-calling layer (set lazily).
        self._tool_bridge = None

        self.setStyleSheet(STYLE)

        self._build_menu()
        self._build_ui()
        self._build_status()

        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.save_shortcut.activated.connect(self.save_current_file)

        # File → Open Folder... (chorded shortcut, VS Code style)
        self.open_folder_shortcut = QShortcut(QKeySequence("Ctrl+K, Ctrl+O"), self)
        self.open_folder_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.open_folder_shortcut.activated.connect(self.open_project_folder)

        self._start_backend_detection()

    # =========================================================
    # Project Root
    # =========================================================

    def project_root(self):
        # <project>/frontend/app/ui/main_window.py
        return Path(__file__).resolve().parents[3]

    def _workspace_path(self) -> Path:
        """Resolved active workspace directory (falls back to repo root)."""
        raw = getattr(self, "active_workspace", None)
        if raw:
            candidate = Path(raw)
            if candidate.is_dir():
                return candidate
        return self.project_root()

    # =========================================================
    # Menus
    # =========================================================

    def _build_menu(self):
        bar = self.menuBar()

        file_menu = bar.addMenu("File")
        for name, slot in [
            ("New File", self.new_file),
            ("Open File...", self.open_file_dialog),
            ("Open Folder...", self.open_project_folder),
            ("Save", self.save_current_file),
            ("Save As...", self.save_file_as),
            ("Exit", self.close),
        ]:
            action = QAction(name, self)
            action.triggered.connect(slot)
            file_menu.addAction(action)

        edit_menu = bar.addMenu("Edit")
        for name, slot in [
            ("Undo", self.undo_editor),
            ("Redo", self.redo_editor),
            ("Cut", self.cut_editor),
            ("Copy", self.copy_editor),
            ("Paste", self.paste_editor),
        ]:
            action = QAction(name, self)
            action.triggered.connect(slot)
            edit_menu.addAction(action)

        view_menu = bar.addMenu("View")
        for name, slot in [
            ("Explorer", self.focus_explorer),
            ("Terminal Launcher", self.focus_terminal),
            ("AI Chat", self.focus_chat),
            ("AI Agent Panel", self.focus_agents),
        ]:
            action = QAction(name, self)
            action.triggered.connect(slot)
            view_menu.addAction(action)

        view_menu.addSeparator()
        for name, slot in [
            ("Build Test Project", self.build_project),
            ("Verify Project", self.verify_project),
            ("Run Agent Pipeline", self.run_agents),
            ("Check Dependencies", self.check_dependencies),
            ("About AutoFix AI Studio", self.show_about),
        ]:
            action = QAction(name, self)
            action.triggered.connect(slot)
            view_menu.addAction(action)

    # =========================================================
    # Status Bar
    # =========================================================

    def _build_status(self):
        status = self.statusBar()
        status.showMessage("Ready")

    def _panel_header(self, title, subtitle=None, refresh_slot=None, extra=None):
        frame = QFrame()
        frame.setObjectName("PanelHeader")

        row = QHBoxLayout(frame)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(8)

        label = QLabel(title)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        row.addWidget(label)

        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Muted")
            row.addWidget(sub)

        row.addStretch()

        if extra is not None:
            row.addWidget(extra)

        if refresh_slot:
            button = QPushButton("↻")
            button.setObjectName("ExplorerRefresh")
            button.setToolTip("Refresh Explorer")
            button.clicked.connect(refresh_slot)
            row.addWidget(button)

        return frame

    # =========================================================
    # Main UI
    #
    # ┌──────────────┬──────────────────────────┬─────────────┐
    # │  EXPLORER    │      CODE EDITOR         │  AI AGENT   │
    # │              ├──────────────────────────┤             │
    # │  TERMINAL    │      AI CHAT             │             │
    # └──────────────┴──────────────────────────┴─────────────┘
    # =========================================================

    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.main_split = QSplitter(Qt.Orientation.Horizontal)
        self.main_split.addWidget(self._left_panel())
        self.main_split.addWidget(self._center_panel())
        self.main_split.addWidget(self._agent_panel())
        self.main_split.setSizes([260, 830, 360])
        self.main_split.setChildrenCollapsible(False)

        outer.addWidget(self.main_split, 1)
        self.setCentralWidget(central)

    def _left_panel(self):
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._explorer_panel())
        split.addWidget(self._terminal_panel())
        split.setSizes([430, 170])
        split.setChildrenCollapsible(False)
        return split

    def _center_panel(self):
        self.center_split = QSplitter(Qt.Orientation.Vertical)
        self.center_split.addWidget(self._editor_panel())
        self.center_split.addWidget(self._chat_panel())
        self.center_split.setSizes([520, 300])
        self.center_split.setChildrenCollapsible(False)
        return self.center_split

    # =========================================================
    # Explorer
    # =========================================================

    def _explorer_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(
            self._panel_header("EXPLORER", "WORKSPACE", self.refresh_explorer)
        )

        self.tree = ExplorerTree()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setAnimated(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.context_menu_requested = self.show_explorer_context_menu
        self.tree.itemDoubleClicked.connect(self.open_explorer_item)

        layout.addWidget(self.tree)
        self.refresh_explorer()
        return panel

    def refresh_explorer(self):
        self.tree.clear()
        root = self._workspace_path()

        if not root.exists():
            return

        root_item = QTreeWidgetItem([root.name])
        root_item.setData(0, self.PATH_ROLE, str(root))
        root_item.setData(0, self.DIR_ROLE, True)

        self.tree.addTopLevelItem(root_item)
        self._populate_directory(root_item, root)
        root_item.setExpanded(True)

    def _populate_directory(self, parent_item, directory):
        ignored = {
            ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", "node_modules", ".idea",
            ".vscode", "dist", "build"
        }

        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except (PermissionError, OSError):
            return

        for path in entries:
            if path.name in ignored:
                continue

            if path.name.startswith(".") and path.name not in {".env", ".gitignore"}:
                continue

            item = QTreeWidgetItem([path.name])
            item.setData(0, self.PATH_ROLE, str(path))
            item.setData(0, self.DIR_ROLE, path.is_dir())
            parent_item.addChild(item)

            if path.is_dir():
                self._populate_directory(item, path)

    def show_explorer_context_menu(self, event):
        item = self.tree.itemAt(event.pos())
        menu = QMenu(self.tree)

        if item is None:
            refresh = menu.addAction("Refresh Explorer")
            selected = menu.exec(event.globalPos())
            if selected == refresh:
                self.refresh_explorer()
            return

        self.tree.setCurrentItem(item)
        raw = item.data(0, self.PATH_ROLE)
        if not raw:
            return

        path = Path(raw)

        new_menu = menu.addMenu("New")
        new_file = new_menu.addAction("File")
        new_folder = new_menu.addAction("Folder")
        menu.addSeparator()

        open_action = menu.addAction("Open")
        rename = menu.addAction("Rename")
        delete = menu.addAction("Delete")
        menu.addSeparator()

        copy_path = menu.addAction("Copy Path")
        refresh = menu.addAction("Refresh Explorer")

        selected = menu.exec(event.globalPos())

        if selected == new_file:
            self.create_explorer_file(path)
        elif selected == new_folder:
            self.create_explorer_folder(path)
        elif selected == open_action:
            self.open_explorer_item(item)
        elif selected == rename:
            self.rename_explorer_item(path)
        elif selected == delete:
            self.delete_explorer_item(path)
        elif selected == copy_path:
            QApplication.clipboard().setText(str(path))
        elif selected == refresh:
            self.refresh_explorer()

    def _directory_for_creation(self, path):
        return path if path.is_dir() else path.parent

    def create_explorer_file(self, selected_path):
        directory = self._directory_for_creation(selected_path)
        name, ok = self.ask_text("New File", "File name:", "untitled.py")

        if not ok or not name.strip():
            return

        target = directory / name.strip()

        if target.exists():
            QMessageBox.warning(self, "File Exists", f"Already exists:\n\n{target}")
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        except OSError as exc:
            QMessageBox.warning(self, "Create File Failed", str(exc))
            return

        self.refresh_explorer()
        self.open_file_path(target)

    def create_explorer_folder(self, selected_path):
        directory = self._directory_for_creation(selected_path)
        name, ok = self.ask_text("New Folder", "Folder name:", "NewFolder")

        if not ok or not name.strip():
            return

        target = directory / name.strip()

        if target.exists():
            QMessageBox.warning(self, "Folder Exists", f"Already exists:\n\n{target}")
            return

        try:
            target.mkdir(parents=True)
        except OSError as exc:
            QMessageBox.warning(self, "Create Folder Failed", str(exc))
            return

        self.refresh_explorer()

    def ask_text(self, title, label, default=""):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(420, 130)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(label))

        input_box = QLineEdit(default)
        layout.addWidget(input_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        input_box.setFocus()
        result = dialog.exec()
        return input_box.text(), result == QDialog.DialogCode.Accepted

    def rename_explorer_item(self, path):
        root = self._workspace_path()

        if path == root:
            QMessageBox.information(self, "Explorer", "The project root cannot be renamed.")
            return

        name, ok = self.ask_text("Rename", "New name:", path.name)

        if not ok or not name.strip() or name.strip() == path.name:
            return

        target = path.parent / name.strip()

        if target.exists():
            QMessageBox.warning(self, "Rename Failed", f"Already exists:\n\n{target}")
            return

        try:
            path.rename(target)
        except OSError as exc:
            QMessageBox.warning(self, "Rename Failed", str(exc))
            return

        self.refresh_explorer()

    def delete_explorer_item(self, path):
        root = self._workspace_path()

        if path == root:
            QMessageBox.information(self, "Explorer", "The project root cannot be deleted.")
            return

        answer = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete this item?\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "Delete Failed", str(exc))
            return

        for index in range(self.tabs.count() - 1, -1, -1):
            widget = self.tabs.widget(index)
            if widget and widget.property("file_path") == str(path):
                self.close_editor_tab(index)

        self.refresh_explorer()

    def open_explorer_item(self, item, column=0):
        raw = item.data(0, self.PATH_ROLE)
        if not raw:
            return

        path = Path(raw)

        if not path.exists():
            self.refresh_explorer()
            return

        if path.is_dir():
            item.setExpanded(not item.isExpanded())
            return

        self.open_file_path(path)

    # =========================================================
    # Editor
    # =========================================================

    def _editor_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_editor_tab)

        layout.addWidget(self.tabs, 1)
        return panel

    def open_file_path(self, path):
        path = Path(path)

        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if widget and widget.property("file_path") == str(path):
                self.tabs.setCurrentIndex(index)
                return

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Unable to Open File",
                f"Could not open:\n\n{path}\n\n{exc}",
            )
            return

        editor = QTextEdit()
        editor.setFont(QFont("Consolas", 11))
        editor.setPlainText(text)
        editor.setProperty("file_path", str(path))
        editor.document().setModified(False)

        self.tabs.addTab(editor, path.name)
        editor.document().modificationChanged.connect(
            lambda modified, e=editor: self.update_editor_tab_title(e, modified)
        )
        self.tabs.setCurrentWidget(editor)
        self.statusBar().showMessage(f"Opened: {path}")

    def update_editor_tab_title(self, editor, modified):
        index = self.tabs.indexOf(editor)
        if index < 0:
            return

        raw_path = editor.property("file_path")
        name = Path(raw_path).name if raw_path else "Untitled"
        self.tabs.setTabText(index, ("● " if modified else "") + name)

    def close_editor_tab(self, index):
        if index < 0 or index >= self.tabs.count():
            return

        widget = self.tabs.widget(index)

        if isinstance(widget, QTextEdit) and widget.document().isModified():
            raw = widget.property("file_path") or "Untitled"

            answer = QMessageBox.question(
                self,
                "Unsaved Changes",
                f"Save changes to {Path(raw).name} before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )

            if answer == QMessageBox.StandardButton.Cancel:
                return

            if answer == QMessageBox.StandardButton.Save:
                self.tabs.setCurrentIndex(index)
                if not self.save_current_file():
                    return

        self.tabs.removeTab(index)
        widget.deleteLater()

    def save_current_file(self):
        index = self.tabs.currentIndex()

        if index < 0:
            self.statusBar().showMessage("No file is open.")
            return False

        editor = self.tabs.widget(index)

        if not isinstance(editor, QTextEdit):
            self.statusBar().showMessage("No editable file is open.")
            return False

        raw_path = editor.property("file_path")

        if not raw_path:
            return self.save_file_as()

        file_path = Path(raw_path)

        try:
            file_path.write_text(editor.toPlainText(), encoding="utf-8")
            editor.document().setModified(False)
            self.tabs.setTabText(index, file_path.name)
            self.statusBar().showMessage(f"Saved: {file_path}")
            self.append_output(f"SAVE  ✓  {file_path}")
            return True
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Unable to Save File",
                f"Could not save:\n\n{file_path}\n\n{exc}",
            )
            return False

    def save_file_as(self):
        index = self.tabs.currentIndex()

        if index < 0:
            self.statusBar().showMessage("No file is open.")
            return False

        editor = self.tabs.widget(index)

        if not isinstance(editor, QTextEdit):
            self.statusBar().showMessage("No editable file is open.")
            return False

        suggested = str(self._workspace_path() / "untitled.py")
        target, _ = QFileDialog.getSaveFileName(
            self, "Save As", suggested, "All Files (*)"
        )

        if not target:
            return False

        try:
            Path(target).write_text(editor.toPlainText(), encoding="utf-8")
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Unable to Save File",
                f"Could not save:\n\n{target}\n\n{exc}",
            )
            return False

        editor.setProperty("file_path", target)
        editor.document().setModified(False)

        index = self.tabs.indexOf(editor)
        if index >= 0:
            self.tabs.setTabText(index, Path(target).name)

        self.statusBar().showMessage(f"Saved: {target}")
        self.append_output(f"SAVE  ✓  {target}")
        return True

    def undo_editor(self):
        editor = self.current_editor()
        if editor:
            editor.undo()

    def redo_editor(self):
        editor = self.current_editor()
        if editor:
            editor.redo()

    def cut_editor(self):
        editor = self.current_editor()
        if editor:
            editor.cut()

    def copy_editor(self):
        editor = self.current_editor()
        if editor:
            editor.copy()

    def paste_editor(self):
        editor = self.current_editor()
        if editor:
            editor.paste()

    def current_editor(self):
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, QTextEdit) else None

    # =========================================================
    # Terminal Launcher (external terminals only)
    # =========================================================

    def _terminal_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._panel_header("TERMINAL", "EXTERNAL"))

        self.terminal_list = QListWidget()
        self.terminal_list.setObjectName("TerminalLauncher")
        self.terminal_list.setIconSize(self.terminal_list.iconSize())

        self._terminal_items = {}
        for kind in ("pwsh", "cmd"):
            item = QListWidgetItem(kind)
            item.setData(self.PATH_ROLE, kind)
            self.terminal_list.addItem(item)
            self._terminal_items[kind] = item

        self.terminal_list.itemClicked.connect(self._on_terminal_item_clicked)
        self._update_terminal_markers()
        layout.addWidget(self.terminal_list, 1)

        hint = QLabel("Click a shell to open it in the active workspace.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        hint.setContentsMargins(8, 2, 8, 6)
        layout.addWidget(hint)

        return panel

    def _update_terminal_markers(self):
        for kind, item in self._terminal_items.items():
            if kind == self.selected_terminal:
                item.setText(f"◀ {kind} ▶")
                item.setSelected(True)
            else:
                item.setText(f"    {kind}")
                item.setSelected(False)

    def _on_terminal_item_clicked(self, item):
        kind = item.data(self.PATH_ROLE)
        if kind not in self._terminal_items:
            return
        self.selected_terminal = kind
        self._update_terminal_markers()
        self.launch_external_terminal(kind)

    def launch_external_terminal(self, kind):
        """Immediately open an external terminal window in the workspace."""
        workspace = self._workspace_path()

        if kind == "pwsh":
            executable = shutil.which("pwsh")
            if not executable:
                message = (
                    "PowerShell 7 (pwsh) was not found on PATH. "
                    "Install PowerShell 7 or use cmd."
                )
                self.statusBar().showMessage(message)
                self.append_output(f"TERMINAL  ✗  {message}")
                return False
        elif kind == "cmd":
            executable = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"
            )
            if not os.path.isfile(executable):
                executable = shutil.which("cmd") or "cmd.exe"
        else:
            self.append_output(f"TERMINAL  ✗  Unknown shell: {kind}")
            return False

        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

        try:
            subprocess.Popen(
                [executable],
                cwd=str(workspace),
                creationflags=creationflags,
            )
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Terminal Launcher",
                f"Could not start {kind}:\n\n{exc}",
            )
            return False

        self.statusBar().showMessage(f"Opened {kind} in {workspace}")
        self.append_output(f"TERMINAL  ▶  {kind} in {workspace}")
        return True

    def focus_terminal(self):
        self.terminal_list.setFocus()

    # =========================================================
    # AI Chat (mode switch: Chat / AutoFix / OpenCode)
    # =========================================================

    def _build_mode_switch(self):
        """Compact segmented mode switch shown in the AI CHAT header."""
        from PySide6.QtWidgets import QButtonGroup

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self._mode_group = QButtonGroup(container)
        self._mode_group.setExclusive(True)

        for mode_key, label in self.AI_MODES:
            button = QPushButton(label)
            button.setObjectName("ModeButton")
            button.setCheckable(True)
            button.setChecked(mode_key == "chat")
            button.setToolTip(self._mode_tooltip(mode_key))
            button.clicked.connect(
                lambda checked=False, key=mode_key: self._on_mode_button_clicked(key)
            )
            self._mode_group.addButton(button)
            self._mode_buttons[mode_key] = button
            row.addWidget(button)

        return container

    @staticmethod
    def _mode_tooltip(mode_key):
        return {
            "chat": "Standalone AI chat — answers, explanations and code "
                    "reviews via the configured Chat AI provider. Never "
                    "creates tasks, switches modes or executes anything",
            "autofix": "AutoFix pipeline — analyze, plan, APPROVE & EXECUTE "
                       "(large pastes are detected automatically)",
        }.get(mode_key, "")

    def _chat_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._chat_panel_layout = layout

        self.chat_workspace_label = QLabel(self._workspace_path().name)
        self.chat_workspace_label.setObjectName("Muted")

        header_extra = QWidget()
        header_row = QHBoxLayout(header_extra)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)
        header_row.addWidget(self._build_mode_switch())
        header_row.addWidget(self.chat_workspace_label)

        layout.addWidget(
            self._panel_header("AI CHAT", None, extra=header_extra)
        )

        self.conversation = QTextEdit()
        self.conversation.setReadOnly(True)
        self.conversation.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.conversation, 1)

        # Non-blocking host for shared-knowledge notifications (Part 5/6):
        # hidden until Chat detects potentially reusable knowledge.
        self._knowledge_host = QWidget()
        self._knowledge_host.setObjectName("KnowledgeHost")
        knowledge_layout = QVBoxLayout(self._knowledge_host)
        knowledge_layout.setContentsMargins(0, 4, 0, 4)
        self._knowledge_host.setVisible(False)
        layout.addWidget(self._knowledge_host)

        self.approve_button = QPushButton("APPROVE & EXECUTE")
        self.approve_button.setObjectName("Approve")
        self.approve_button.clicked.connect(self.on_approve_plan)
        # Always visible in Chat AND AutoFix (and every other mode): it only
        # ever changes between disabled (no actionable plan) and enabled.
        self.approve_button.setVisible(True)
        self._set_approve_enabled(False)
        layout.addWidget(self.approve_button)

        self.stop_button = QPushButton("STOP")
        self.stop_button.setObjectName("Stop")
        self.stop_button.clicked.connect(self.on_stop_execution)
        self.stop_button.setVisible(False)
        layout.addWidget(self.stop_button)

        self.chat_input = ChatInput()
        self.chat_input.setFixedHeight(72)
        self._apply_mode_placeholder()
        self.chat_input.send_requested.connect(self.on_chat_send)
        layout.addWidget(self.chat_input)

        self._append_chat(
            "AI",
            "AutoFix Assistant ready.\n"
            "Mode: Chat — just talk to me normally. I answer questions, "
            "explain code and review snippets with your configured Chat AI "
            "provider. Chat never creates tasks or executes anything.\n"
            "Switch modes above: AutoFix analyzes, plans and executes "
            "development tasks with your approval.",
        )

        return panel

    def _set_approve_enabled(self, enabled: bool):
        """The approval gate lives in the enabled state, never visibility."""
        self.approve_button.setEnabled(bool(enabled))
        self.approve_button.setToolTip(
            "Approve the displayed plan and execute the AutoFix pipeline"
            if enabled
            else "No actionable plan yet — AutoFix (or a Bulk paste) enables "
                 "this once a plan awaits your approval"
        )

    def _apply_mode_placeholder(self):
        placeholders = {
            "chat": "Chat with the assistant…  (Enter to send, Shift+Enter for a newline)",
            "autofix": "Describe a coding task or paste a large code block…  "
                       "(Enter to send, Shift+Enter for a newline)",
        }
        self.chat_input.setPlaceholderText(
            placeholders.get(self.current_ai_mode, placeholders["chat"])
        )

    def _on_mode_button_clicked(self, mode_key):
        if mode_key == self.current_ai_mode:
            return

        self.current_ai_mode = mode_key
        self._apply_mode_placeholder()

        # APPROVE & EXECUTE stays visible in every mode; its enabled state
        # tracks the pending plan only.  Mode switches never touch it.

        label = dict(self.AI_MODES).get(mode_key, mode_key)
        self._append_chat("System", f"Mode: {label}")

    def set_ai_mode(self, mode_key):
        """Programmatic mode switch (keeps the whole conversation)."""
        button = self._mode_buttons.get(mode_key)
        if button is None:
            return False
        button.setChecked(True)
        self._on_mode_button_clicked(mode_key)
        return True

    def _remember_message(self, role, text):
        self._conversation_history.append((role, str(text)))
        if len(self._conversation_history) > 16:
            del self._conversation_history[:-16]

    def _append_chat(self, speaker, text):
        colors = {"You": "#4b91ff", "AI": "#35c28f", "System": "#8993a5"}
        color = colors.get(speaker, "#e6eaf0")
        escaped = (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        self.conversation.append(
            f"<span style='color:{color};font-weight:700'>{speaker}</span><br>{escaped}<br>"
        )

    # ── Routing ────────────────────────────────────────────────
    #
    # Deterministic per-send dispatch: the mode selected at send time
    # decides the handler.  Nothing else may change the route.
    #
    #   Chat     → Chat provider only (never AutoFix / Bulk / OpenCode)
    #   AutoFix  → existing AutoFix pipeline (plan → approve → execute)
    #   Bulk     → AutoFix pipeline automatically (input mode, no transfer)
    #   OpenCode → explicit OpenCode CLI workflow

    def on_chat_send(self, text):
        text = (text or "").strip()
        if not text:
            return

        self._append_chat("You", text)
        self._last_request = text
        # Only Chat exchanges belong to the chat-provider conversation
        # history — AutoFix and internal worker contexts stay separate.
        if self.current_ai_mode == "chat":
            self._remember_message("user", text)

            # Deterministic, offline pre-classification. Large build specs
            # keep flowing straight into the existing AutoFix planning flow
            # (task transport and persistence unchanged). Normal-sized coding
            # requests stay in Chat as revisable proposals — still gated by
            # APPROVE & EXECUTE; Chat itself never executes anything.
            try:
                from app.agents.intent import classify_intent
                from app.agents.large_input import is_large_input

                intent = classify_intent(text)
                large = is_large_input(text)
            except Exception:
                intent = None
                large = False

            if intent is not None and intent.is_coding_task:
                # Project-bound work needs a valid workspace either way.
                try:
                    workspace = self._workspace_path()
                except Exception:
                    workspace = None
                if workspace is None or not Path(workspace).is_dir():
                    self._append_chat(
                        "System",
                        "Workspace is unavailable — cannot create an AutoFix task.",
                    )
                    return

                if large:
                    self._append_chat(
                        "System",
                        "Large task detected — routing to AutoFix to prepare a plan for your approval.",
                    )
                    self.set_ai_mode("autofix")
                    self._handle_autofix_message(text)
                    return

            self._handle_chat_message(text)
            return

        # Clear any short-label from previous large-input runs unless this send
        # is intentionally feeding a bulk-style intake into AutoFix.
        if self.current_ai_mode != "bulk":
            self._short_label = None

        handlers = {
            "chat": self._handle_chat_message,
            "autofix": self._handle_autofix_message,
            "bulk": self._handle_bulk_message,
            "opencode": self._handle_opencode_message,
        }
        handler = handlers.get(self.current_ai_mode)
        if handler is not None:
            handler(text)

    # ── Mode 1: conversational chat (Chat intelligence layer) ────
    #
    # Every message goes through ChatEngine: natural replies for discussion,
    # revisable AUTOFIX PROPOSALs for coding/project requests, clarification
    # only when materially necessary, and approval detection that hands ONE
    # exact execution prompt to the existing pipeline.  Chat NEVER creates
    # tasks by itself, never executes anything, and never invokes OpenCode.

    def _handle_chat_message(self, text):
        self._append_chat("System", "Thinking…")
        self.statusBar().showMessage("Preparing reply…")

        if self._chat_worker_thread is not None and self._chat_worker_thread.isRunning():
            self._chat_worker_thread.cancel()

        worker = ChatWorker(
            text,
            str(self._workspace_path()),
            history=list(self._conversation_history[:-1]),
            parent=self,
        )
        # Set as an attribute (not an __init__ kwarg) so test doubles that
        # override __init__ keep working.
        worker.active_proposal = (
            dict(self._active_chat_proposal) if self._active_chat_proposal else None
        )
        worker.reply_ready.connect(self._on_chat_reply)
        worker.reply_failed.connect(self._on_chat_error)
        worker.structured_ready.connect(self._on_structured_reply)
        worker.research_status.connect(self._on_research_status)
        self._chat_worker_thread = worker
        worker.start()

    def _on_research_status(self, status):
        """Subtle research status in the status bar — non-blocking."""
        if status == "researching":
            self.statusBar().showMessage("Researching current information…")
        elif status == "complete":
            self.statusBar().showMessage("Research complete — preparing reply…")

    def _on_structured_reply(self, payload):
        """State transitions from the ChatEngine's structured response."""
        kind = payload.get("kind")

        if kind in ("proposal", "revision"):
            proposal = payload.get("proposal") or {}
            self._active_chat_proposal = proposal
            request = (
                payload.get("original_request")
                or proposal.get("origin_request")
                or self._last_request
                or ""
            )
            try:
                from app.agents.chat_intelligence import (
                    ChatProposal,
                    render_proposal_text,
                )

                plan_text = render_proposal_text(ChatProposal.from_dict(proposal))
            except Exception:
                plan_text = proposal.get("execution_prompt", "")
            self._pending_plan = {
                "id": uuid.uuid4().hex[:8],
                "request": request,
                "plan": plan_text,
                "task": None,
                "short_label": getattr(self, "_short_label", None),
                "execution_prompt": proposal.get("execution_prompt", ""),
                "context": {
                    "origin": "chat-proposal",
                    "revisions": len(proposal.get("revisions", [])),
                },
            }
            self._render_proposal_card(proposal, request)
            self.approve_button.setVisible(True)
            self._set_approve_enabled(True)

        elif kind == "approval":
            self.on_approve_plan()

        elif payload.get("requires_clarification"):
            # The question itself was appended via reply_ready — nothing
            # else to mutate; no proposal state changes.
            pass

        if payload.get("knowledge_proposal"):
            self._show_knowledge_notification(payload["knowledge_proposal"])

    # ── Shared-knowledge discovery (non-blocking notification) ────

    def _show_knowledge_notification(self, proposal: dict):
        """'New AI knowledge detected.' card with Review/Save/Ignore.

        Non-blocking by design: Chat stays usable, nothing is saved until the
        user presses Save to GitHub, and Ignore simply discards the candidate.
        """
        from app.agents.knowledge_security import detect_secret_types

        body = str(proposal.get("body", ""))
        findings = detect_secret_types(body)
        if findings:
            # Never display secret material — show a safe placeholder.
            body = "[Security scan: " + ", ".join(findings) + " detected — " \
                   "this knowledge cannot be saved as-is.]"

        self._knowledge_payload = dict(proposal)

        host = self._knowledge_host

        # Rebuild the single card cleanly each time.
        old = host.findChild(QWidget, "KnowledgeCard")
        if old is not None:
            old.setParent(None)
            old.deleteLater()

        card = QWidget()
        card.setObjectName("KnowledgeCard")
        row = QVBoxLayout(card)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(6)

        heading = QLabel("New AI knowledge detected.")
        heading.setStyleSheet("font-weight:700; color:#fbbf24;")
        row.addWidget(heading)

        meta = QLabel(
            f"Category: {proposal.get('category', 'lessons')}    "
            f"Confidence: {int(round(float(proposal.get('confidence', 0)) * 100))}%"
        )
        meta.setObjectName("Muted")
        row.addWidget(meta)

        title_label = QLabel(f"Title: {proposal.get('title', '')}")
        title_label.setWordWrap(True)
        row.addWidget(title_label)

        body_edit = QTextEdit()
        body_edit.setReadOnly(True)
        body_edit.setPlainText(body)
        body_edit.setMaximumHeight(96)
        row.addWidget(body_edit)

        source_label = QLabel(f"Source: {proposal.get('source', 'Chat conversation')}")
        source_label.setObjectName("Muted")
        source_label.setWordWrap(True)
        row.addWidget(source_label)

        buttons = QHBoxLayout()
        review_btn = QPushButton("Review")
        save_btn = QPushButton("Save to GitHub")
        ignore_btn = QPushButton("Ignore")
        buttons.addWidget(review_btn)
        buttons.addWidget(save_btn)
        buttons.addWidget(ignore_btn)
        buttons.addStretch(1)
        row.addLayout(buttons)

        review_btn.clicked.connect(self._on_knowledge_review)
        save_btn.clicked.connect(self._on_knowledge_save)
        ignore_btn.clicked.connect(self._on_knowledge_ignore)

        self._knowledge_body_edit = body_edit
        self._knowledge_review_btn = review_btn
        self._knowledge_save_btn = save_btn
        self._knowledge_ignore_btn = ignore_btn
        self._knowledge_heading = heading

        host.layout().addWidget(card)
        host.setVisible(True)
        self.statusBar().showMessage("New AI knowledge detected.", 5000)
        self.append_output("[knowledge] New AI knowledge detected.")

    def _on_knowledge_review(self):
        """Inspect/edit the proposed knowledge before any saving happens."""
        edit = getattr(self, "_knowledge_body_edit", None)
        if edit is None:
            return
        currently_editable = not edit.isReadOnly()
        edit.setReadOnly(currently_editable)
        self._knowledge_review_btn.setText(
            "Done reviewing" if not currently_editable else "Review"
        )
        self._knowledge_heading.setText(
            "New AI knowledge detected. (reviewing)" if not currently_editable
            else "New AI knowledge detected."
        )

    def _on_knowledge_ignore(self):
        """Discard the proposal without saving anything."""
        self._cancel_knowledge_save_worker()
        self._knowledge_payload = None
        self._knowledge_host.setVisible(False)
        self.statusBar().showMessage("Knowledge suggestion ignored.")

    def _on_knowledge_save(self):
        """EXPLICIT user approval → attempt the GitHub save (off-thread)."""
        if not getattr(self, "_knowledge_payload", None):
            return
        edit = getattr(self, "_knowledge_body_edit", None)
        if edit is not None and not edit.isReadOnly():
            # Review edits become the saved content (still security-vetted).
            self._knowledge_payload["body"] = edit.toPlainText()

        from app.agents.github_knowledge import knowledge_status

        status = knowledge_status()
        if not status.get("configured"):
            hint = status.get("hint") or (
                "Set AUTOFIX_KNOWLEDGE_REPO and GITHUB_TOKEN."
            )
            self._append_chat(
                "System",
                "Shared knowledge repository is not configured.\n" + hint,
            )
            self.statusBar().showMessage("Knowledge repository not configured")
            return

        self._set_knowledge_buttons_enabled(False)
        worker = KnowledgeSaveWorker(self._knowledge_payload, parent=self)
        worker.saved.connect(self._on_knowledge_saved)
        self._knowledge_save_worker = worker
        self.statusBar().showMessage("Saving AI knowledge to GitHub…")
        worker.start()

    def _cancel_knowledge_save_worker(self):
        worker = self._knowledge_save_worker
        if worker is not None and worker.isRunning():
            worker.cancel()

    def _set_knowledge_buttons_enabled(self, enabled: bool):
        for name in (
            "_knowledge_review_btn", "_knowledge_save_btn", "_knowledge_ignore_btn",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(bool(enabled))

    def _on_knowledge_saved(self, ok: bool, message: str):
        self._set_knowledge_buttons_enabled(True)
        self._knowledge_save_worker = None
        self.append_output(f"[knowledge] {message}")
        if ok:
            self._append_chat("System", message)   # "...saved to GitHub."
            self._knowledge_payload = None
            self._knowledge_host.setVisible(False)
            self.statusBar().showMessage("AI knowledge saved to GitHub.")
        else:
            # Honest failure: keep the card so the user can retry or ignore.
            self._append_chat("System", f"{message}")
            self.statusBar().showMessage("AI knowledge could not be saved")

    def _on_chat_reply(self, reply):
        self.statusBar().showMessage("Ready")
        self._append_chat("AI", reply)
        self._remember_message("assistant", reply)

    def _on_chat_error(self, detail):
        self.statusBar().showMessage("AI Chat Error")
        # Defense in depth: never display credential material from a
        # provider error (the provider layer already redacts; re-check here).
        try:
            from app.agents.task_memory import redact_secrets

            detail = redact_secrets(str(detail or ""))
        except Exception:
            detail = str(detail or "")
        self._append_chat(
            "System",
            "AI Chat Error\n"
            "Unable to reach the configured AI provider."
            + (f"\n{detail}" if detail else ""),
        )

    # ── Mode 2: AutoFix agent (plan → approve → execute) ──────
    #
    # Large pastes are detected automatically — no separate mode.  The
    # complete input is preserved end-to-end and recorded under
    # <workspace>\.autofix\memory\ so an interrupted run can be recovered.

    def _handle_autofix_message(self, text):
        workspace = self._workspace_path()
        self._active_task_memory = None

        # Always attempt to produce a short one-word label from large
        # contiguous multi-line inputs (20+ lines). This label is purely a
        # compact UI hint — it must never replace or truncate the original
        # request (which always reaches the planner and task memory).
        try:
            try:
                self._short_label = compress_task(text)
            except Exception:
                self._short_label = None
        except Exception:
            self._short_label = None

        if is_large_input(text):
            # Persist the complete request to the task memory and include the
            # short_label (if any) as metadata.
            try:
                extra = {}
                if getattr(self, "_short_label", None):
                    extra["short_label"] = self._short_label
                self._active_task_memory = save_task_record(
                    workspace, text, status="received", **extra
                )
            except OSError:
                self._active_task_memory = None
            summary = summarize_large_input(text)
            self._append_chat("System", f"AutoFix\nProcessing large input…\n{summary}")
            self.statusBar().showMessage("AutoFix: analyzing large task…")
        else:
            self._append_chat("System", "Analyzing the workspace and preparing a plan…")

        if self._plan_worker is not None and self._plan_worker.isRunning():
            self._plan_worker.cancel()

        worker = PlanWorker(text, str(workspace), self)
        worker.plan_ready.connect(self._on_plan_ready)
        worker.plan_failed.connect(self._on_plan_failed)
        self._plan_worker = worker
        worker.start()

    def _render_proposal_card(self, plan_text, request_text=None):
        """Render a clear proposal view that makes the approval gate explicit.

        Accepts either a structured chat-proposal dict (Chat intelligence
        layer) or a plan string (AutoFix planner output) — both render into
        the same visual card so approval always looks identical.
        """
        if isinstance(plan_text, dict):
            self._render_structured_card(plan_text)
            return

        objective = (request_text or self._last_request or "Coding task").strip()
        plan_html = str(plan_text or "Plan to be determined.")
        plan_lines = [line.strip() for line in plan_html.splitlines() if line.strip()]
        if not plan_lines:
            plan_lines = ["1. Assess the request", "2. Implement the fix", "3. Verify the result"]

        # Keep the visible proposal readable without replacing the actual
        # execution prompt or the durable task record.
        if len(plan_lines) > 6:
            plan_lines = plan_lines[:6]

        ordered = "".join(
            f"<li>{self._escape_html(line)}</li>" for line in plan_lines
        )
        block = (
            "<div style='margin:8px 0; padding:12px 14px; border:1px solid #2a3343; "
            "border-radius:8px; background:#111827; color:#e6eaf0;'>"
            "<div style='font-weight:700; letter-spacing:0.08em; color:#7dd3fc; margin-bottom:8px;'>"
            "AUTOFIX PROPOSAL</div>"
            "<div style='font-weight:600; margin-bottom:6px;'>Objective</div>"
            f"<div style='margin-bottom:12px; white-space:pre-wrap;'>{self._escape_html(objective)}</div>"
            "<div style='font-weight:600; margin-bottom:6px;'>Plan</div>"
            f"<ol style='margin:0 0 12px 20px; padding-left:12px;'>{ordered}</ol>"
            "<div style='color:#aeb7c6; margin-bottom:8px;'>Execution prompt prepared.</div>"
            "<div style='font-weight:700; color:#fbbf24;'>AWAITING APPROVAL</div>"
            "</div>"
        )
        self.conversation.append(block)

    def _render_structured_card(self, proposal):
        """Rich card for a structured ChatProposal dict."""

        def section(title, body_html, is_list=False):
            if not body_html:
                return ""
            inner = body_html if is_list else (
                f"<div style='margin:0 0 12px; white-space:pre-wrap;'>{body_html}</div>"
            )
            return (
                f"<div style='font-weight:600; margin-bottom:4px;'>{title}</div>{inner}"
            )

        def list_html(items, numbered=False):
            rows = [self._escape_html(str(item)) for item in items]
            if not rows:
                return ""
            tag = "ol" if numbered else "ul"
            joined = "".join(f"<li>{row}</li>" for row in rows)
            return (
                f"<{tag} style='margin:0 0 12px 20px; padding-left:12px;'>{joined}</{tag}>"
            )

        objective = str(proposal.get("objective", "")).strip()
        understanding = str(proposal.get("understanding", "")).strip()
        analysis = str(proposal.get("analysis_summary", "")).strip()
        revisions = proposal.get("revisions") or []
        status = str(proposal.get("status", "AWAITING APPROVAL"))

        revision_note = ""
        if revisions:
            revision_note = section(
                f"Revisions ({len(revisions)})",
                list_html(revisions),
                is_list=True,
            )

        html = "".join(filter(None, [
            section("Objective", self._escape_html(objective)),
            section("Understanding", self._escape_html(understanding)),
            section("Current Architecture / Analysis", self._escape_html(analysis)),
            revision_note,
            section("Implementation Plan", list_html(proposal.get("plan") or [], numbered=True)),
            section("Files / Components", list_html(proposal.get("affected_components") or [])),
            section("Dependencies", list_html(proposal.get("dependencies") or [])),
            section("Risks", list_html(proposal.get("risks") or [])),
            section("Verification", list_html(proposal.get("verification_plan") or [], numbered=True)),
            ("<div style='color:#aeb7c6; margin-bottom:8px;'>"
             "Execution prompt prepared.</div>"),
            f"<div style='font-weight:700; color:#fbbf24;'>{self._escape_html(status)}</div>",
        ]))
        block = (
            "<div style='margin:8px 0; padding:12px 14px; border:1px solid #2a3343; "
            "border-radius:8px; background:#111827; color:#e6eaf0;'>"
            "<div style='font-weight:700; letter-spacing:0.08em; color:#7dd3fc; "
            "margin-bottom:8px;'>AUTOFIX PROPOSAL</div>"
            + html +
            "</div>"
        )
        self.conversation.append(block)

    @staticmethod
    def _escape_html(value):
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _on_plan_ready(self, plan_text):
        # PlanWorker emits a string; tolerate dict payloads defensively.
        task_record = None
        if isinstance(plan_text, dict):
            task_record = plan_text.get("task")
            plan_text = plan_text.get("plan") or str(plan_text)

        self._render_proposal_card(plan_text, self._last_request)
        if self._active_task_memory is not None:
            self._append_chat(
                "System",
                "Large task — the complete input is preserved and will be "
                f"executed in full. Memory: {self._active_task_memory}",
            )
            try:
                update_task_record(
                    self._active_task_memory, status="planned", plan=plan_text
                )
            except OSError:
                pass
        self._append_chat(
            "AI",
            "Review the plan above. Press APPROVE & EXECUTE to run it — "
            "nothing runs without your approval.",
        )
        self._pending_plan = {
            "id": uuid.uuid4().hex[:8],
            "request": self._last_request,
            "plan": plan_text,
            "task": task_record,
            "short_label": getattr(self, "_short_label", None),
        }
        # If a short label exists, surface it as a compact UI hint (do not
        # replace or remove the original request text).
        if self._pending_plan.get("short_label"):
            self._append_chat("System", f"Task label: {self._pending_plan['short_label']}")

        self.approve_button.setVisible(True)
        self._set_approve_enabled(True)

    def _on_plan_failed(self, message):
        self._append_chat("AI", f"I could not produce a plan.\n{message}")

    # ── Large-input intake path ───────────────────────────────────
    #
    # Large or bulk-style requests are accepted directly into the AutoFix
    # workflow without creating a separate execution engine. The complete
    # request is preserved verbatim and the APPROVE & EXECUTE gate remains
    # active until the user approves the generated plan.

    def _handle_bulk_message(self, text):
        # Generate a short single-word label for large multi-line Bulk inputs
        # (20+ lines). This label is only a compact UI/title — the original
        # request is preserved verbatim and passed to the planner/execution.
        try:
            self._short_label = compress_task(text)
        except Exception:
            self._short_label = None

        self._append_chat(
            "System",
            "Bulk → AutoFix\n"
            "Input accepted automatically — analyzing the workspace and "
            "preparing a plan for your approval.",
        )
        self.statusBar().showMessage("Bulk: routed into AutoFix…")
        self._handle_autofix_message(text)

    # ── Mode 4: OpenCode ───────────────────────────────────────

    def _handle_opencode_message(self, text):
        workspace = self._workspace_path()
        self._append_chat(
            "System", f"Sending request to OpenCode in {workspace.name}…"
        )
        self.statusBar().showMessage("OpenCode is working…")

        if self._chat_worker_thread is not None and self._chat_worker_thread.isRunning():
            self._chat_worker_thread.cancel()

        worker = OpenCodeChatWorker(text, str(workspace), parent=self)
        # OpenCode workers provided by the test harness may be simple spies
        # without Qt Signal attributes. Guard connects to avoid AttributeError
        # while preserving normal behavior for real workers.
        try:
            worker.output_received.connect(self.append_output)
        except Exception:
            pass
        try:
            worker.request_finished.connect(self._on_opencode_finished)
        except Exception:
            pass
        self._chat_worker_thread = worker
        worker.start()

    def _on_opencode_finished(self, ok, message):
        self.statusBar().showMessage("Ready")
        if ok:
            # OpenCode output stays out of the Chat provider history —
            # conversations are kept separated by mode.
            self._append_chat("AI", f"OpenCode:\n{message}")
        else:
            self._append_chat(
                "System",
                "OpenCode Unavailable\n"
                "OpenCode could not be started or reached."
                + (f"\n{message}" if message else ""),
            )

    def on_approve_plan(self):
        """Approval is tied to the currently displayed plan and its button."""
        if not self._pending_plan:
            return

        if self._pipeline is not None and self._pipeline.isRunning():
            return

        plan = self._pending_plan
        request = plan.get("request") or self._last_request or ""
        # Chat-approved proposals hand the EXACT approved execution prompt
        # to AutoFix; direct-AutoFix plans keep using the original request.
        execution_prompt = plan.get("execution_prompt") or request
        workspace = str(self._workspace_path())

        task_record = plan.get("task")
        if task_record is None:
            task_record = AutoFixTask.create(workspace, request)
            plan["task"] = task_record
        task_record.approved_prompt = execution_prompt
        if plan.get("plan"):
            task_record.plan = plan.get("plan")
        task_record.save()

        self._pending_plan = None
        self._active_chat_proposal = None
        # Button stays visible; it is simply not actionable until the next
        # plan is produced.
        self._set_approve_enabled(False)
        self.stop_button.setVisible(True)

        self._append_chat("System", "APPROVED")
        self._append_chat(
            "System",
            "AutoFix is now executing task "
            f"{task_record.task_id}\nSubmitting to the existing pipeline: "
            "decompose → assign agents → WorkerRouter → verify.",
        )
        # Preserve the architecture decision for future Chat context
        # (existing project memory system — redacted, no raw logs).
        try:
            from app.agents.task_memory import KIND_DECISIONS, record_memory

            record_memory(
                workspace,
                KIND_DECISIONS,
                f"approved:{request[:60]}",
                (
                    f"Approved via Chat proposal. Objective: {request} "
                    f"Execution prompt preserved on task {task_record.task_id}."
                ),
                tags=["approval", "chat-proposal"],
            )
        except Exception:
            pass
        self.reset_agent_statuses()
        self.statusBar().showMessage("AutoFix: Executing")

        if self._active_task_memory is not None:
            try:
                update_task_record(self._active_task_memory, status="executing")
            except OSError:
                pass

        # Live diagnostic activity for the right-side panel.
        self.set_diagnostic_status("APPROVED")
        self.append_output("Plan approved")
        self.append_output(f"AutoFix task created: {task_record.task_id}")
        self.append_output("Creating job queue...")

        pipeline = ApprovalPipeline(
            execution_prompt,
            workspace,
            approved_plan=plan.get("plan"),
            existing_task=task_record,
            context_metadata=plan.get("context"),
            parent=self,
        )
        pipeline.stage_started.connect(self._on_stage_started)
        pipeline.stage_finished.connect(self._on_stage_finished)
        pipeline.coding_output.connect(self._on_coding_output)
        pipeline.status_changed.connect(self._on_pipeline_status)
        pipeline.worker_notification.connect(self._on_worker_notification)
        pipeline.pipeline_finished.connect(self._on_pipeline_finished)
        self._worker_notify_seen = set()
        self._pipeline = pipeline
        pipeline.start()

    def _on_pipeline_status(self, text):
        first_line = text.splitlines()[0] if text else ""
        self.statusBar().showMessage(first_line)
        if first_line == "AutoFix: Recovering":
            self.set_diagnostic_status("RECOVERING")
        elif first_line == "AutoFix: Verifying":
            self.set_diagnostic_status("VERIFYING")
        elif first_line == "AutoFix: Planning":
            self.set_diagnostic_status("PLANNING")
        for line in text.splitlines():
            self.append_output(line)

    def on_stop_execution(self):
        """User cancellation — CANCELLED, never auto-restarted."""
        pipeline = self._pipeline
        if pipeline is None or not pipeline.isRunning():
            return
        self._append_chat("System", "Stopping AutoFix execution…")
        self.statusBar().showMessage("AutoFix: Cancelling…")
        pipeline.cancel()

    _STAGE_TO_STATE = {
        "Planner": "PLANNING",
        "Coding": "CODING",
        "Tester": "TESTING",
        "Debugger": "DEBUGGING",
        "Reviewer": "REVIEWING",
        "Verification": "VERIFYING",
    }

    def _on_stage_started(self, label):
        self.set_agent_status(label, "Running")
        state = self._STAGE_TO_STATE.get(label)
        if state:
            self.set_diagnostic_status(state)
        self.append_output(f"{label} started")

    def _on_stage_finished(self, label, ok, message):
        self.set_agent_status(label, "✓" if ok else "✗")
        first_line = message.splitlines()[0] if message else ""
        verb = "completed" if ok else "failed"
        self.append_output(f"{label} {verb}: {first_line}")

        if self._active_task_memory is not None:
            try:
                update_task_record(
                    self._active_task_memory,
                    append_stage={"stage": label, "ok": ok, "message": first_line},
                )
            except OSError:
                pass

    def _on_coding_output(self, text):
        self.append_output(text)

    def _on_worker_notification(self, payload: dict):
        """Non-blocking worker auth/configuration notifications.

        Observability only: AutoFix keeps running (fallback already happened
        inside the WorkerRouter). Payload contains safe metadata only — no
        credentials ever reach this layer.
        """
        event = payload.get("event_type") or ""
        severity = payload.get("severity") or "info"
        message = payload.get("message") or ""

        # Execution-area line for every event (low-noise surface).
        self.append_output(f"[worker] {message}")

        if event == "no_worker_available":
            detail = payload.get("detail") or ""
            block = "✖ No AutoFix worker is available."
            if detail:
                block += f"\n{detail}"
            self.statusBar().showMessage("AutoFix: No available worker")
            self._append_chat("System", block)
            return

        if severity != "warning":
            # Plain unavailability stays in the output panel — informational.
            return

        # Auth/config warnings are visible but non-blocking. Identical
        # warnings within one task run are shown once to avoid chat spam;
        # the set is cleared when a pipeline starts/finishes.
        if message in self._worker_notify_seen:
            self.statusBar().showMessage(message)
            return
        self._worker_notify_seen.add(message)
        self.statusBar().showMessage(message)
        self._append_chat(
            "System",
            f"⚠ {message}\nAutoFix is continuing with the next available worker.",
        )

    def _on_pipeline_finished(self, success, summary):
        self._worker_notify_seen = set()
        self._append_chat("AI", summary)
        state = getattr(self._pipeline, "final_state", None)
        status_text = {
            "COMPLETED": "AutoFix: Completed",
            "CANCELLED": "AutoFix: Cancelled",
            "RECOVERY_REQUIRED": "AutoFix: Recovery Required",
        }.get(state, "Execution PASSED" if success else "Execution FAILED")
        self.statusBar().showMessage(status_text)

        diagnostic = {
            "COMPLETED": "COMPLETED",
            "CANCELLED": "CANCELLED",
            "RECOVERY_REQUIRED": "RECOVERY_REQUIRED",
        }.get(state, "COMPLETED" if success else "FAILED")
        self.set_diagnostic_status(diagnostic)
        self.append_output(
            "Verification PASSED" if success else "Verification FAILED"
        )
        self.stop_button.setVisible(False)
        backend = getattr(self._pipeline, "backend_used", None)
        self.set_backend_label(backend)

        if self._active_task_memory is not None:
            record = {
                "status": "completed" if success else "failed",
                "verified": bool(success),
                "backend": backend,
            }
            if not success:
                record["append_error"] = summary
            try:
                update_task_record(self._active_task_memory, **record)
                if not success:
                    saved = load_task_record(self._active_task_memory) or {}
                    update_task_record(
                        self._active_task_memory,
                        remaining=describe_remaining(saved),
                    )
            except OSError:
                pass
            if not success:
                self._append_chat(
                    "System",
                    "Large task state saved for recovery:\n"
                    f"{self._active_task_memory}\n"
                    "The original request is preserved — approve & execute "
                    "it again once the issue is addressed.",
                )
            self._active_task_memory = None

    # =========================================================
    # AI Agent Panel
    # =========================================================

    def _agent_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._panel_header("AI AGENT", "ORCHESTRATOR"))

        self.agent_list = QListWidget()
        self.agent_list.setFont(QFont("Consolas", 10))
        for stage in self.AGENT_STAGES:
            item = QListWidgetItem(f"{stage:<14}Waiting")
            item.setData(self.AGENT_NAME_ROLE, stage)
            self.agent_list.addItem(item)
        layout.addWidget(self.agent_list, 1)

        layout.addWidget(self._panel_header("AI CHAT AGENTS"))

        # Chat agents plan/reason; coding agents execute. Circle colour is
        # the ONLY colored element — all text stays normal panel text.
        self.chat_agent_labels = {}
        for agent in ("GPT", "Claude", "DeepSeek"):
            label = QLabel(f"● {agent}")
            label.setObjectName("Muted")
            label.setContentsMargins(12, 1, 10, 1)
            layout.addWidget(label)
            self.chat_agent_labels[agent] = label

        layout.addWidget(self._panel_header("CODING AGENTS"))

        self.coding_agent_labels = {}
        for backend in ("opencode", "openhands", "continue", "aider"):
            label = QLabel(f"● {backend.capitalize()}")
            label.setObjectName("Muted")
            label.setContentsMargins(12, 1, 10, 1)
            layout.addWidget(label)
            self.coding_agent_labels[backend] = label

        self.primary_agent_label = QLabel("PRIMARY: —")
        self.primary_agent_label.setObjectName("Muted")
        self.primary_agent_label.setContentsMargins(12, 3, 10, 2)
        layout.addWidget(self.primary_agent_label)

        self.backend_label = QLabel("Coding backend: detecting…")
        self.backend_label.setObjectName("Muted")
        self.backend_label.setContentsMargins(10, 4, 10, 4)
        layout.addWidget(self.backend_label)

        layout.addWidget(self._panel_header("DIAGNOSTIC"))

        self.diagnostic_status_label = QLabel("STATUS: WAITING")
        self.diagnostic_status_label.setObjectName("Muted")
        self.diagnostic_status_label.setContentsMargins(10, 3, 10, 3)
        layout.addWidget(self.diagnostic_status_label)

        self.diagnostics = QTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setFont(QFont("Consolas", 9))
        self.diagnostics.setPlaceholderText("Output, problems, tests and verification…")
        layout.addWidget(self.diagnostics, 1)

        return panel

    def set_agent_status(self, stage, status):
        for index in range(self.agent_list.count()):
            item = self.agent_list.item(index)
            if item.data(self.AGENT_NAME_ROLE) == stage:
                item.setText(f"{stage:<14}{status}")
                return

    def reset_agent_statuses(self):
        for index in range(self.agent_list.count()):
            item = self.agent_list.item(index)
            stage = item.data(self.AGENT_NAME_ROLE)
            item.setText(f"{stage:<14}Waiting")

    def set_backend_label(self, backend):
        if backend == "opencode":
            self.backend_label.setText("Coding backend: OpenCode")
        elif backend == "openhands":
            self.backend_label.setText("Coding backend: OpenHands")
        elif backend == "continue":
            self.backend_label.setText("Coding backend: Continue")
        elif backend == "aider":
            self.backend_label.setText("Coding backend: Aider")
        elif backend == "none":
            self.backend_label.setText("Coding backend: none available")
        else:
            self.backend_label.setText("Coding backend: —")

    _STATUS_GREEN = "#35c28f"
    _STATUS_RED = "#e05561"
    _AGENT_DISPLAY_NAMES = {
        "opencode": "OpenCode",
        "openhands": "OpenHands",
        "continue": "Continue",
        "aider": "Aider",
    }

    @staticmethod
    def _status_row_html(name, state, color, detail=""):
        """One indicator row: colored circle ONLY, plain text afterwards."""
        row = (
            f"<span style='color:{color};'>&#9679;</span>"
            f" {name}&nbsp;&nbsp;{state}"
        )
        if detail:
            row += f"&nbsp;&nbsp;({detail})"
        return row

    def _paint_status_label(self, label, name, available, detail=""):
        color = self._STATUS_GREEN if available else self._STATUS_RED
        state = "AVAILABLE" if available else "UNAVAILABLE"
        label.setText(self._status_row_html(name, state, color, detail))

    def update_coding_agent_statuses(self, backends):
        """Refresh the CODING AGENTS indicators from a {name: BackendInfo} map.

        Missing entries are treated as unavailable — a partial detection
        result must never crash the panel.  Only the status circle is
        colored; agent names and states stay normal panel text.
        """
        primary = None
        for name in ("opencode", "openhands", "continue", "aider"):
            label = self.coding_agent_labels.get(name)
            if label is None:
                continue
            info = backends.get(name)
            available = bool(info and info.available and info.executable)
            detail = (getattr(info, "detail", "") or "").strip()
            if not available:
                detail = ""
            self._paint_status_label(
                label, self._AGENT_DISPLAY_NAMES[name], available, detail
            )
            if available and primary is None:
                primary = name

        if primary:
            self.primary_agent_label.setText(
                f"PRIMARY: {self._AGENT_DISPLAY_NAMES[primary]}"
            )
        else:
            self.primary_agent_label.setText("NO CODING AGENT AVAILABLE")
        self.primary_agent_label.setStyleSheet("")

    def update_chat_agent_statuses(self, agents):
        """Refresh the AI CHAT AGENTS indicators from a {name: ChatAgentInfo} map."""
        for name in ("GPT", "Claude", "DeepSeek"):
            label = self.chat_agent_labels.get(name)
            if label is None:
                continue
            info = agents.get(name)
            available = bool(info and info.available)
            detail = (getattr(info, "detail", "") or "").strip()
            if not available:
                detail = ""
            self._paint_status_label(label, name, available, detail)

    DIAGNOSTIC_STATES = (
        "WAITING", "ANALYZING", "PLANNING", "APPROVED", "QUEUED",
        "CODING", "TESTING", "DEBUGGING", "REVIEWING", "VERIFYING",
        "COMPLETED", "FAILED", "RECOVERING", "CANCELLED",
        "RECOVERY_REQUIRED",
    )

    def set_diagnostic_status(self, state):
        state = str(state).upper()
        if state not in self.DIAGNOSTIC_STATES:
            return
        self.diagnostic_status_label.setText(f"STATUS: {state}")

    def _start_backend_detection(self):
        from PySide6.QtCore import QThread

        class _Worker(QThread):
            detected = Signal(dict)

            def run(inner_self):
                runner = CodingAgentRunner()
                try:
                    inner_self.detected.emit(runner.detect_backends())
                except Exception:
                    inner_self.detected.emit({})

        self._backend_worker = _Worker(self)
        self._backend_worker.detected.connect(self._on_backends_detected)
        self._backend_worker.start()

        class _ChatWorker(QThread):
            detected = Signal(dict)

            def run(inner_self):
                from app.agents.chat_agents import detect_chat_agents

                try:
                    inner_self.detected.emit(detect_chat_agents())
                except Exception:
                    inner_self.detected.emit({})

        self._chat_worker = _ChatWorker(self)
        self._chat_worker.detected.connect(self.update_chat_agent_statuses)
        self._chat_worker.start()

    def _on_backends_detected(self, backends):
        try:
            self.update_coding_agent_statuses(backends)
        except Exception:
            pass
        from app.agents.coding_agent import PRIORITY_ORDER

        for name in PRIORITY_ORDER:
            info = backends.get(name)
            if info and info.available and info.executable:
                self.set_backend_label(name)
                return
        self.set_backend_label("none")

    def append_output(self, text):
        self.diagnostics.append(str(text))

    def focus_chat(self):
        self.chat_input.setFocus()

    def focus_agents(self):
        self.agent_list.setFocus()

    def focus_explorer(self):
        self.tree.setFocus()

    # =========================================================
    # Workspace management
    # =========================================================

    def set_active_workspace(self, path):
        """Make *path* the authoritative IDE workspace and propagate it."""
        if path is None or not str(path).strip():
            return False

        raw = str(path)
        previous_workspace = getattr(self, "active_workspace", None)

        self.active_workspace = raw
        self.project_path = raw
        self.agent_workspace = raw

        if hasattr(self, "refresh_explorer"):
            try:
                self.refresh_explorer()
            except Exception:
                pass

        explorer = getattr(self, "explorer", None)
        if explorer is not None:
            for method_name in (
                "set_root_path",
                "set_project_path",
                "set_workspace",
                "update_folder",
            ):
                method = getattr(explorer, method_name, None)
                if callable(method):
                    try:
                        method(raw)
                        break
                    except Exception:
                        pass

        for component_name in (
            "agent",
            "agent_panel",
            "opencode",
            "opencode_terminal",
            "aider",
        ):
            component = getattr(self, component_name, None)
            if component is None:
                continue
            for method_name in (
                "set_workspace",
                "set_project_path",
                "set_working_directory",
            ):
                method = getattr(component, method_name, None)
                if callable(method):
                    try:
                        method(raw)
                        break
                    except Exception:
                        pass

        if hasattr(self, "chat_workspace_label"):
            try:
                self.chat_workspace_label.setText(Path(raw).name)
            except Exception:
                pass

        try:
            self.setWindowTitle(f"AutoFix AI Studio — {Path(raw).name}")
        except Exception:
            pass

        self.statusBar().showMessage(f"Workspace: {raw}")
        return True

    def open_project_folder(self):
        """File → Open Folder... — the only way to change the project.

        Mirrors the proven ``open_file_dialog`` pattern: parent = MainWindow,
        str start directory, truthiness check, then hand off to the existing
        workspace handler.
        """
        folder = QFileDialog.getExistingDirectory(
            self,
            "Open Project Folder",
            str(self._workspace_path()),
        )

        if folder:
            self.set_active_workspace(folder)

    def new_file(self):
        name, ok = self.ask_text("New File", "File name:", "untitled.py")
        if not ok or not name.strip():
            return

        target = self._workspace_path() / name.strip()

        if target.exists():
            QMessageBox.warning(self, "File Exists", f"Already exists:\n\n{target}")
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        except OSError as exc:
            QMessageBox.warning(self, "Create File Failed", str(exc))
            return

        self.open_file_path(target)
        self.refresh_explorer()

    def open_file_dialog(self):
        target, _ = QFileDialog.getOpenFileName(
            self, "Open File", str(self._workspace_path()), "All Files (*)"
        )
        if target:
            self.open_file_path(target)

    # =========================================================
    # Agents / Build / Verify
    # =========================================================

    def run_agents(self):
        self.append_output("AGENTS  ▶  Pipeline started")

        role_map = {
            "Planner": "Planner",
            "Coder": "Coding",
            "Tester": "Tester",
            "Debugger": "Debugger",
            "Reviewer": "Reviewer",
            "Verification": "Verification",
        }

        try:
            results = AgentOrchestrator().run(
                {"project": str(self._workspace_path())}
            )
        except Exception as exc:
            self.append_output(f"AGENTS  ✗  {exc}")
            return

        for result in results:
            stage = role_map.get(result.agent)
            if stage:
                self.set_agent_status(stage, "✓" if result.status.value == "passed" else "✗")
            else:
                self.append_output(
                    f"AGENT  •  {result.agent}: {result.message.splitlines()[0]}"
                )

        self._append_chat(
            "AI",
            "Legacy agent pipeline completed. "
            "Use the chat to request an approved, verified execution.",
        )
        self.append_output("AGENTS  ✓  Pipeline complete")

    def build_project(self):
        workspace = self.project_root() / "workspace"
        workspace.mkdir(exist_ok=True)

        self.append_output("BUILD  ▶  Starting...")

        try:
            result = ProjectBuilder().build_python_project(
                workspace,
                "AutoFix_Test_Project",
            )
        except Exception as exc:
            self.append_output(f"BUILD  ✗  {exc}")
            return

        if result.success:
            self.project_path = str(result.project_path)
            self.append_output("BUILD  ✓  " + result.message)

            for path in result.files_created:
                self.append_output("  + " + str(path))

            self.refresh_explorer()
        else:
            self.append_output("BUILD  !  " + result.message)

    def verify_project(self):
        project = Path(self.project_path or "")

        if not project.exists():
            project = self._workspace_path()

        if not project.exists():
            self.append_output("VERIFY  !  No project available to verify.")
            return

        self.append_output("VERIFY  ▶  Starting...")

        try:
            errors = ProjectVerifier().verify(project)
        except Exception as exc:
            self.append_output(f"VERIFY  ✗  {exc}")
            return

        if errors:
            self.append_output("VERIFY  ✗  FAILED")
            for error in errors:
                self.append_output("  " + str(error))
        else:
            self.append_output("VERIFY  ✓  PASSED — structure and Python syntax valid")

    # =========================================================
    # Dependency Checker
    # =========================================================

    def check_dependencies(self):
        try:
            statuses = DependencyChecker().check_all()
        except Exception as exc:
            QMessageBox.critical(self, "Dependency Checker", str(exc))
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("AutoFix Dependency Checker")
        dialog.resize(720, 430)

        layout = QVBoxLayout(dialog)
        title = QLabel("Dependency Check")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        layout.addWidget(title)

        table = QTableWidget(len(statuses), 4)
        table.setHorizontalHeaderLabels(
            ["Dependency", "Status", "Version", "Action"]
        )
        table.horizontalHeader().setStretchLastSection(True)

        for row, status in enumerate(statuses):
            table.setItem(row, 0, QTableWidgetItem(status.spec.name))
            table.setItem(
                row,
                1,
                QTableWidgetItem(
                    "✓ Installed" if status.installed else "✗ Missing"
                ),
            )
            table.setItem(
                row,
                2,
                QTableWidgetItem(status.version or "—"),
            )

            if not status.installed:
                button = QPushButton("Install")
                button.clicked.connect(
                    lambda checked=False, st=status, d=dialog:
                    self.install_dependency(st, d)
                )
                table.setCellWidget(row, 3, button)
            else:
                table.setItem(row, 3, QTableWidgetItem("Ready"))

        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        dialog.exec()

    def install_dependency(self, status, parent_dialog):
        spec = status.spec

        answer = QMessageBox.question(
            self,
            "Dependency Missing",
            f"{spec.name} was not found.\n\n"
            "Do you want AutoFix AI Studio to install it?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            ok, message = DependencyChecker().install_python_package(spec)
        except Exception as exc:
            ok, message = False, str(exc)

        if ok:
            QMessageBox.information(self, "Dependency Installed", message)
            parent_dialog.accept()
            self.check_dependencies()
        else:
            QMessageBox.warning(self, "Installation Not Available", message)

    # =========================================================
    # Misc
    # =========================================================

    def show_about(self):
        QMessageBox.information(
            self,
            "About AutoFix AI Studio",
            "AutoFix AI Studio\n\n"
            "VS Code-inspired IDE • Multi-Agent Engineering • Verification\n\n"
            "Coding backends (automatic priority):\n"
            "OpenCode → OpenHands → Continue → Aider",
        )

    # =========================================================
    # Cleanup
    # =========================================================

    def closeEvent(self, event: QCloseEvent):
        """Stop background workers cleanly; never touch deleted C++ objects."""
        for attr in (
            "_plan_worker",
            "_pipeline",
            "_backend_worker",
            "_chat_worker",
            "_chat_worker_thread",
        ):
            worker = getattr(self, attr, None)
            if worker is None:
                continue

            try:
                cancel = getattr(worker, "cancel", None)
                if callable(cancel):
                    try:
                        cancel()
                    except RuntimeError:
                        pass

                if worker.isRunning():
                    if not worker.wait(3000):
                        # Last resort so Qt never destroys a running thread.
                        worker.terminate()
                        worker.wait(1000)
            except RuntimeError:
                pass

        event.accept()


def create_app():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    return app, window
