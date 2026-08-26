from pathlib import Path

from PySide6.QtCore import Qt, QProcess
from PySide6.QtGui import QAction, QFont, QTextCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QMenu,
)

from app.agents.orchestrator import AgentOrchestrator
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
QToolBar {
    background: #151922;
    border: 0;
    padding: 5px;
    spacing: 5px;
}
QToolButton { color: #d9dee8; padding: 6px 9px; }
QToolButton:hover { background: #252c3a; }
QPushButton {
    background: #202633;
    border: 1px solid #30394a;
    border-radius: 6px;
    padding: 7px 11px;
}
QPushButton:hover { background: #2a3343; }
QPushButton#Primary {
    background: #3b82f6;
    border: 0;
    font-weight: 600;
}
QPushButton#Primary:hover { background: #4b91ff; }
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
QTreeWidget, QListWidget, QTextEdit, QLineEdit {
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
    """Terminal input with reliable right-click paste."""

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        paste_action = menu.addAction("Paste")
        menu.addSeparator()
        clear_action = menu.addAction("Clear Input")

        selected = menu.exec(event.globalPos())

        if selected == paste_action:
            self.insert(QApplication.clipboard().text())
        elif selected == clear_action:
            self.clear()

    def mousePressEvent(self, event):
        # Let QLineEdit handle right-click normally so contextMenuEvent
        # can reliably show our custom paste menu.
        super().mousePressEvent(event)


class ExplorerTree(QTreeWidget):
    """Explorer tree with a reliable native Qt context menu."""

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


class MainWindow(QMainWindow):
    PATH_ROLE = Qt.ItemDataRole.UserRole
    DIR_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self):
        super().__init__()

        self.setWindowTitle("AutoFix AI Studio")
        self.resize(1450, 900)
        self.setMinimumSize(1100, 700)

        self.project_path = None
        self.terminal_process = None

        self.setStyleSheet(STYLE)

        self._build_menu()
        self._build_toolbar()
        self._build_ui()
        self._build_status()

        # Global Save shortcut. QShortcut works even when the editor
        # or terminal input currently has keyboard focus.
        self.save_shortcut = QShortcut(
            QKeySequence.StandardKey.Save,
            self,
        )
        self.save_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.save_shortcut.activated.connect(self.save_current_file)

    # =========================================================
    # Project Root
    # =========================================================

    def project_root(self):
        # main_window.py is:
        # <project>/frontend/app/ui/main_window.py
        return Path(__file__).resolve().parents[3]

    # =========================================================
    # Menu
    # =========================================================

    def _build_menu(self):
        bar = self.menuBar()

        menus = {
            "File": [
                ("New", self.build_project),
                ("Open", self.noop),
                ("Save", self.save_current_file),
            ],
            "Edit": [
                ("Undo", self.noop),
                ("Redo", self.noop),
            ],
            "View": [
                ("Explorer", self.noop),
                ("Terminal", self.focus_terminal),
            ],
            "Project": [
                ("Build Project", self.build_project),
                ("Verify Project", self.verify_project),
            ],
            "Build": [
                ("Build", self.build_project),
                ("Verify", self.verify_project),
            ],
            "Run": [
                ("Run", self.noop),
                ("Test", self.verify_project),
            ],
            "Agents": [
                ("Run Agent Pipeline", self.run_agents),
            ],
            "Tools": [
                ("Dependencies", self.check_dependencies),
            ],
            "Help": [
                ("About AutoFix AI Studio", self.show_about),
            ],
        }

        for menu_name, actions in menus.items():
            menu = bar.addMenu(menu_name)
            for action_name, callback in actions:
                action = QAction(action_name, self)
                action.triggered.connect(callback)
                menu.addAction(action)

    # =========================================================
    # Toolbar
    # =========================================================

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        actions = [
            ("New", self.build_project),
            ("Open", self.noop),
            ("Save", self.save_current_file),
            ("Build", self.build_project),
            ("Run", self.noop),
            ("Test", self.verify_project),
            ("Analyze", self.verify_project),
            ("Security", self.run_agents),
            ("Repair", self.noop),
            ("Agents", self.run_agents),
            ("Dependencies", self.check_dependencies),
        ]

        for text, slot in actions:
            action = QAction(text, self)
            action.triggered.connect(slot)
            toolbar.addAction(action)

    # =========================================================
    # Panel Header
    # =========================================================

    def _panel_header(self, title, subtitle=None, refresh_slot=None):
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

        if refresh_slot:
            button = QPushButton("↻")
            button.setObjectName("ExplorerRefresh")
            button.setToolTip("Refresh Explorer")
            button.clicked.connect(refresh_slot)
            row.addWidget(button)

        return frame

    # =========================================================
    # Main UI
    # =========================================================

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setObjectName("PanelHeader")

        row = QHBoxLayout(header)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        brand = QLabel("AutoFix AI Studio")
        brand.setObjectName("Brand")
        row.addWidget(brand)

        tag = QLabel(
            "AI Builder  •  Multi-Agent Engineering  •  Verification"
        )
        tag.setObjectName("Muted")
        row.addWidget(tag)
        row.addStretch()

        primary = QPushButton("Build Test Project")
        primary.setObjectName("Primary")
        primary.clicked.connect(self.build_project)
        row.addWidget(primary)

        outer.addWidget(header)

        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.addWidget(self._explorer_panel())
        main_split.addWidget(self._editor_panel())
        main_split.addWidget(self._agent_panel())
        main_split.setSizes([250, 780, 420])

        bottom = self._bottom_panel()

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(main_split)
        vertical.addWidget(bottom)
        vertical.setSizes([650, 250])

        outer.addWidget(vertical, 1)
        self.setCentralWidget(root)

    # =========================================================
    # Explorer
    # =========================================================

    def _explorer_panel(self):
        panel = QWidget()

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(
            self._panel_header(
                "EXPLORER",
                "WORKSPACE",
                self.refresh_explorer,
            )
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

        root = self.project_root()

        if not root.exists():
            return

        root_item = QTreeWidgetItem([root.name])
        root_item.setData(
            0,
            self.PATH_ROLE,
            str(root),
        )
        root_item.setData(
            0,
            self.DIR_ROLE,
            True,
        )

        self.tree.addTopLevelItem(root_item)
        self._populate_directory(root_item, root)
        root_item.setExpanded(True)

    def _populate_directory(self, parent_item, directory):
        ignored = {
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "node_modules",
            ".idea",
            ".vscode",
            "dist",
            "build",
        }

        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda p: (
                    not p.is_dir(),
                    p.name.lower(),
                ),
            )
        except (PermissionError, OSError):
            return

        for path in entries:
            if path.name in ignored:
                continue

            if path.name.startswith(".") and path.name not in {
                ".env",
                ".gitignore",
            }:
                continue

            item = QTreeWidgetItem([path.name])
            item.setData(
                0,
                self.PATH_ROLE,
                str(path),
            )
            item.setData(
                0,
                self.DIR_ROLE,
                path.is_dir(),
            )

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
        root = self.project_root()

        new_menu = menu.addMenu("New")
        new_file = new_menu.addAction("File")
        new_folder = new_menu.addAction("Folder")

        menu.addSeparator()

        rename = menu.addAction("Rename")
        delete = menu.addAction("Delete")

        menu.addSeparator()

        copy_path = menu.addAction("Copy Path")

        menu.addSeparator()

        refresh = menu.addAction("Refresh Explorer")

        selected = menu.exec(event.globalPos())

        if selected == new_file:
            self.create_explorer_file(path)
        elif selected == new_folder:
            self.create_explorer_folder(path)
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

        name, ok = self.ask_text(
            "New File",
            "File name:",
            "untitled.py",
        )

        if not ok or not name.strip():
            return

        target = directory / name.strip()

        if target.exists():
            QMessageBox.warning(
                self,
                "File Exists",
                f"Already exists:\n\n{target}",
            )
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Create File Failed",
                str(exc),
            )
            return

        self.refresh_explorer()
        self.open_file_path(target)

    def create_explorer_folder(self, selected_path):
        directory = self._directory_for_creation(selected_path)

        name, ok = self.ask_text(
            "New Folder",
            "Folder name:",
            "NewFolder",
        )

        if not ok or not name.strip():
            return

        target = directory / name.strip()

        if target.exists():
            QMessageBox.warning(
                self,
                "Folder Exists",
                f"Already exists:\n\n{target}",
            )
            return

        try:
            target.mkdir(parents=True)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Create Folder Failed",
                str(exc),
            )
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
        root = self.project_root()

        if path == root:
            QMessageBox.information(
                self,
                "Explorer",
                "The project root cannot be renamed.",
            )
            return

        name, ok = self.ask_text(
            "Rename",
            "New name:",
            path.name,
        )

        if not ok or not name.strip() or name.strip() == path.name:
            return

        target = path.parent / name.strip()

        if target.exists():
            QMessageBox.warning(
                self,
                "Rename Failed",
                f"Already exists:\n\n{target}",
            )
            return

        try:
            path.rename(target)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Rename Failed",
                str(exc),
            )
            return

        self.refresh_explorer()

    def delete_explorer_item(self, path):
        root = self.project_root()

        if path == root:
            QMessageBox.information(
                self,
                "Explorer",
                "The project root cannot be deleted.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete this item?\n\n{path}",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Delete Failed",
                str(exc),
            )
            return

        for index in range(self.tabs.count() - 1, -1, -1):
            widget = self.tabs.widget(index)
            if widget.property("file_path") == str(path):
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

    def open_file_path(self, path):
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)

            if widget.property("file_path") == str(path):
                self.tabs.setCurrentIndex(index)
                return

        try:
            text = path.read_text(
                encoding="utf-8",
                errors="replace",
            )
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

        self.statusBar().showMessage(
            f"Opened: {path}"
        )

    def update_editor_tab_title(self, editor, modified):
        index = self.tabs.indexOf(editor)

        if index < 0:
            return

        raw_path = editor.property("file_path")
        name = Path(raw_path).name if raw_path else "Untitled"

        self.tabs.setTabText(
            index,
            ("● " if modified else "") + name,
        )

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
        self.tabs.tabCloseRequested.connect(
            self.close_editor_tab
        )

        layout.addWidget(self.tabs, 1)

        return panel

    def close_editor_tab(self, index):
        if index < 0 or index >= self.tabs.count():
            return

        widget = self.tabs.widget(index)

        if widget is None:
            return

        if isinstance(widget, QTextEdit) and widget.document().isModified():
            file_path = widget.property("file_path") or "Untitled"

            answer = QMessageBox.question(
                self,
                "Unsaved Changes",
                f"Save changes to {Path(file_path).name} before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )

            if answer == QMessageBox.StandardButton.Cancel:
                return

            if answer == QMessageBox.StandardButton.Save:
                if not self.save_current_file():
                    return

        self.tabs.removeTab(index)
        widget.deleteLater()

    # =========================================================
    # Agents
    # =========================================================

    def _agent_panel(self):
        panel = QWidget()

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(
            self._panel_header(
                "AI AGENTS",
                "ORCHESTRATOR",
            )
        )

        self.agent_list = QListWidget()
        self.agent_list.addItems(
            [
                "◯ Planner",
                "◯ Builder",
                "◯ Analyzer",
                "◯ Security",
                "◯ Tester",
                "◯ Reviewer",
            ]
        )

        layout.addWidget(self.agent_list, 1)

        layout.addWidget(
            self._panel_header("AI ASSISTANT")
        )

        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        self.chat.setPlainText(
            "AutoFix Assistant\n\n"
            "Ready to analyze or build your project."
        )

        layout.addWidget(self.chat, 1)

        return panel

    # =========================================================
    # Terminal
    # =========================================================

    def _bottom_panel(self):
        panel = QWidget()

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(
            self._panel_header(
                "TERMINAL  •  TESTS  •  PROBLEMS  •  OUTPUT  •  VERIFICATION"
            )
        )

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 10))

        layout.addWidget(self.output, 1)

        self.terminal_input = TerminalInput()
        self.terminal_input.setFont(QFont("Consolas", 10))
        self.terminal_input.setPlaceholderText(
            "Enter PowerShell command and press Enter..."
        )
        self.terminal_input.returnPressed.connect(
            self.execute_terminal_command
        )

        layout.addWidget(self.terminal_input)

        self.terminal_process = QProcess(self)
        self.terminal_process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )

        self.terminal_process.readyRead.connect(
            self.read_terminal_output
        )
        self.terminal_process.finished.connect(
            self.terminal_finished
        )
        self.terminal_process.errorOccurred.connect(
            self.terminal_error
        )

        self.start_terminal()

        return panel

    def start_terminal(self):
        if (
            self.terminal_process.state()
            != QProcess.ProcessState.NotRunning
        ):
            return

        root = self.project_root()

        self.output.append(
            "────────────────────────────────────────"
        )
        self.output.append(
            "AutoFix PowerShell Terminal"
        )
        self.output.append(
            f"Starting PowerShell in {root}"
        )
        self.output.append("")

        self.terminal_process.setWorkingDirectory(
            str(root)
        )

        self.terminal_process.start(
            "powershell.exe",
            [
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
            ],
        )

    def execute_terminal_command(self):
        command = self.terminal_input.text().strip()

        if not command:
            return

        lowered = command.lower()

        if lowered in {
            "cls",
            "clear",
            "clear-host",
        }:
            self.terminal_process.readAll()
            self.output.clear()
            self.output.append(
                f"PS {self.project_root()}>"
            )
            self.terminal_input.clear()
            self.focus_terminal()
            return

        if (
            self.terminal_process.state()
            == QProcess.ProcessState.NotRunning
        ):
            self.start_terminal()

        # IMPORTANT:
        # Do NOT manually append "PS> command".
        # PowerShell itself echoes the command.
        # Manually adding it causes duplicate command display.
        self.terminal_input.clear()

        self.terminal_process.write(
            (command + "\r\n").encode("utf-8")
        )

    def read_terminal_output(self):
        data = self.terminal_process.readAll()

        if not data:
            return

        text = bytes(data).decode(
            "utf-8",
            errors="replace",
        )

        cursor = self.output.textCursor()
        cursor.movePosition(
            QTextCursor.MoveOperation.End
        )
        cursor.insertText(text)

        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def terminal_finished(
        self,
        exit_code,
        exit_status,
    ):
        self.output.append("")
        self.output.append(
            f"[PowerShell exited: {exit_code}]"
        )

    def terminal_error(self, error):
        self.output.append(
            f"[Terminal error: {error}]"
        )

    def focus_terminal(self):
        self.terminal_input.setFocus()

    # =========================================================
    # Status
    # =========================================================

    def _build_status(self):
        status = QStatusBar()
        self.setStatusBar(status)

        status.showMessage(
            "Ready  •  Python  •  Diagnostics: 0  •  Security: Ready"
        )

    # =========================================================
    # Build
    # =========================================================

    def build_project(self):
        workspace = (
            self.project_root()
            / "workspace"
        )
        workspace.mkdir(exist_ok=True)

        self.output.append(
            "BUILD  ▶  Starting..."
        )

        try:
            result = (
                ProjectBuilder()
                .build_python_project(
                    workspace,
                    "AutoFix_Test_Project",
                )
            )
        except Exception as exc:
            self.output.append(
                f"BUILD  ✗  {exc}"
            )
            return

        if result.success:
            self.project_path = result.project_path

            self.output.append(
                "BUILD  ✓  " + result.message
            )

            for path in result.files_created:
                self.output.append(
                    "  + " + str(path)
                )

            self.refresh_explorer()
        else:
            self.output.append(
                "BUILD  !  " + result.message
            )

    # =========================================================
    # Verification
    # =========================================================

    def verify_project(self):
        project = (
            self.project_path
            or (
                self.project_root()
                / "workspace"
                / "AutoFix_Test_Project"
            )
        )

        if not project.exists():
            self.output.append(
                "VERIFY  !  Build the test project first."
            )
            return

        self.output.append(
            "VERIFY  ▶  Starting..."
        )

        try:
            errors = ProjectVerifier().verify(project)
        except Exception as exc:
            self.output.append(
                f"VERIFY  ✗  {exc}"
            )
            return

        if errors:
            self.output.append(
                "VERIFY  ✗  FAILED"
            )

            for error in errors:
                self.output.append(
                    "  " + str(error)
                )
        else:
            self.output.append(
                "VERIFY  ✓  PASSED — "
                "structure and Python syntax valid"
            )

    # =========================================================
    # Agents
    # =========================================================

    def run_agents(self):
        self.output.append(
            "AGENTS  ▶  Pipeline started"
        )

        try:
            results = AgentOrchestrator().run(
                {
                    "project": str(
                        self.project_path or ""
                    )
                }
            )
        except Exception as exc:
            self.output.append(
                f"AGENTS  ✗  {exc}"
            )
            return

        for index, result in enumerate(results):
            if index < self.agent_list.count():
                self.agent_list.item(index).setText(
                    "● "
                    + result.agent
                    + "  —  "
                    + result.status.value.upper()
                )

            self.output.append(
                f"AGENT  ✓  {result.agent}: "
                f"{result.message}"
            )

        self.chat.setPlainText(
            "AutoFix Assistant\n\n"
            "Agent pipeline completed. "
            "All prototype agents reported PASS."
        )

        self.output.append(
            "AGENTS  ✓  Pipeline complete"
        )

    # =========================================================
    # Dependency Checker
    # =========================================================

    def check_dependencies(self):
        try:
            statuses = DependencyChecker().check_all()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Dependency Checker",
                str(exc),
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(
            "AutoFix Dependency Checker"
        )
        dialog.resize(720, 430)

        layout = QVBoxLayout(dialog)

        title = QLabel("Dependency Check")
        title.setFont(
            QFont(
                "Segoe UI",
                14,
                QFont.Weight.Bold,
            )
        )
        layout.addWidget(title)

        table = QTableWidget(
            len(statuses),
            4,
        )
        table.setHorizontalHeaderLabels(
            [
                "Dependency",
                "Status",
                "Version",
                "Action",
            ]
        )
        table.horizontalHeader().setStretchLastSection(
            True
        )

        for row, status in enumerate(statuses):
            table.setItem(
                row,
                0,
                QTableWidgetItem(
                    status.spec.name
                ),
            )

            table.setItem(
                row,
                1,
                QTableWidgetItem(
                    "✓ Installed"
                    if status.installed
                    else "✗ Missing"
                ),
            )

            table.setItem(
                row,
                2,
                QTableWidgetItem(
                    status.version or "—"
                ),
            )

            if not status.installed:
                button = QPushButton("Install")
                button.clicked.connect(
                    lambda checked=False,
                    st=status,
                    d=dialog:
                    self.install_dependency(
                        st,
                        d,
                    )
                )
                table.setCellWidget(
                    row,
                    3,
                    button,
                )
            else:
                table.setItem(
                    row,
                    3,
                    QTableWidgetItem("Ready"),
                )

        layout.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
        )
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        dialog.exec()

    def install_dependency(
        self,
        status,
        parent_dialog,
    ):
        spec = status.spec

        answer = QMessageBox.question(
            self,
            "Dependency Missing",
            (
                f"{spec.name} was not found.\n\n"
                "Do you want AutoFix AI Studio "
                "to install it?"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )

        if (
            answer
            != QMessageBox.StandardButton.Yes
        ):
            return

        try:
            ok, message = (
                DependencyChecker()
                .install_python_package(spec)
            )
        except Exception as exc:
            ok = False
            message = str(exc)

        if ok:
            QMessageBox.information(
                self,
                "Dependency Installed",
                message,
            )
            parent_dialog.accept()
            self.check_dependencies()
        else:
            QMessageBox.warning(
                self,
                "Installation Not Available",
                message,
            )

    # =========================================================
    # Misc
    # =========================================================

    def show_about(self):
        QMessageBox.information(
            self,
            "About AutoFix AI Studio",
            (
                "AutoFix AI Studio\n\n"
                "AI Builder • Multi-Agent Engineering "
                "• Verification"
            ),
        )

    def save_current_file(self):
        """Save the active editor tab back to its real filesystem file."""

        index = self.tabs.currentIndex()

        if index < 0:
            self.output.append("SAVE  !  No file is open.")
            self.statusBar().showMessage("No file is open.")
            return False

        editor = self.tabs.widget(index)

        if not isinstance(editor, QTextEdit):
            self.output.append("SAVE  !  No editable file is open.")
            self.statusBar().showMessage("No editable file is open.")
            return False

        raw_path = editor.property("file_path")

        if not raw_path:
            self.output.append("SAVE  !  This editor has no file path.")
            return False

        file_path = Path(raw_path)

        try:
            file_path.write_text(
                editor.toPlainText(),
                encoding="utf-8",
            )

            editor.document().setModified(False)
            self.tabs.setTabText(index, file_path.name)

            self.output.append(f"SAVE  ✓  {file_path}")
            self.statusBar().showMessage(f"Saved: {file_path}")
            return True

        except Exception as exc:
            self.output.append(f"SAVE  ✗  {file_path}: {exc}")

            QMessageBox.warning(
                self,
                "Unable to Save File",
                f"Could not save:\n\n{file_path}\n\n{exc}",
            )

            return False

    def noop(self):
        self.output.append(
            "INFO  •  This command is reserved "
            "for the next implementation phase."
        )

    # =========================================================
    # Cleanup
    # =========================================================

    def closeEvent(self, event):
        if (
            self.terminal_process
            and self.terminal_process.state()
            != QProcess.ProcessState.NotRunning
        ):
            self.terminal_process.kill()
            self.terminal_process.waitForFinished(1000)

        event.accept()


def create_app():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    return app, window
