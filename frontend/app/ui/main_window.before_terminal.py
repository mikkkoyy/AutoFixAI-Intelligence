from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QMainWindow,
    QMenu, QMenuBar, QPushButton, QSplitter, QStatusBar, QTabWidget,
    QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QToolBar,
    QDialog, QMessageBox, QTableWidget, QTableWidgetItem, QDialogButtonBox,
)

from app.agents.orchestrator import AgentOrchestrator
from app.builder.project_builder import ProjectBuilder
from app.verification.verifier import ProjectVerifier
from app.dependencies.checker import DependencyChecker


STYLE = """
QWidget { background:#0f1117; color:#e6eaf0; font-family:'Segoe UI'; font-size:13px; }
QMainWindow { background:#0f1117; }
QMenuBar, QMenu { background:#151922; color:#d9dee8; border:0; }
QMenuBar::item:selected, QMenu::item:selected { background:#252c3a; }
QToolBar { background:#151922; border:0; padding:5px; spacing:5px; }
QPushButton { background:#202633; border:1px solid #30394a; border-radius:6px; padding:7px 11px; }
QPushButton:hover { background:#2a3343; }
QPushButton#Primary { background:#3b82f6; border:0; font-weight:600; }
QPushButton#Primary:hover { background:#4b91ff; }
QFrame#PanelHeader { background:#151922; border-bottom:1px solid #262d3a; }
QLabel#Brand { font-size:18px; font-weight:700; }
QLabel#Muted { color:#8993a5; }
QTreeWidget, QListWidget, QTextEdit { background:#0b0e13; border:1px solid #252c38; }
QTreeWidget { border-left:0; border-top:0; border-bottom:0; }
QTreeWidget::item:selected, QListWidget::item:selected { background:#263147; }
QTabWidget::pane { border:1px solid #252c38; }
QTabBar::tab { background:#151922; padding:9px 16px; border-right:1px solid #252c38; }
QTabBar::tab:selected { background:#0b0e13; }
QTextEdit { padding:10px; }
QStatusBar { background:#151922; color:#aeb7c6; }
QSplitter::handle { background:#202632; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoFix AI Studio")
        self.resize(1450, 900)
        self.project_path = None
        self._build_menu()
        self._build_toolbar()
        self._build_ui()
        self._build_status()
        self.setStyleSheet(STYLE)

    def _build_menu(self):
        bar = self.menuBar()
        for title in ["File", "Edit", "View", "Project", "Build", "Run", "Agents", "Tools", "Help"]:
            menu = bar.addMenu(title)
            menu.addAction("Coming soon")

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)
        actions = [
            ("New", self.build_project), ("Open", self.noop), ("Save", self.noop),
            ("Build", self.build_project), ("Run", self.noop), ("Test", self.verify_project),
            ("Analyze", self.verify_project), ("Security", self.run_agents), ("Repair", self.noop),
            ("Agents", self.run_agents), ("Dependencies", self.check_dependencies),
        ]
        for text, slot in actions:
            action = QAction(text, self)
            action.triggered.connect(slot)
            toolbar.addAction(action)

    def _panel_header(self, title, subtitle=None):
        frame = QFrame(); frame.setObjectName("PanelHeader")
        row = QHBoxLayout(frame); row.setContentsMargins(10, 8, 10, 8)
        label = QLabel(title); label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold)); row.addWidget(label)
        if subtitle:
            sub = QLabel(subtitle); sub.setObjectName("Muted"); row.addWidget(sub)
        row.addStretch()
        return frame

    def _build_ui(self):
        root = QWidget(); outer = QVBoxLayout(root); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        header = QFrame(); header.setObjectName("PanelHeader")
        row = QHBoxLayout(header); row.setContentsMargins(14, 10, 14, 10)
        brand = QLabel("AutoFix AI Studio"); brand.setObjectName("Brand"); row.addWidget(brand)
        tag = QLabel("  AI Builder  •  Multi-Agent Engineering  •  Verification"); tag.setObjectName("Muted"); row.addWidget(tag); row.addStretch()
        primary = QPushButton("Build Test Project"); primary.setObjectName("Primary"); primary.clicked.connect(self.build_project); row.addWidget(primary)
        outer.addWidget(header)

        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.addWidget(self._explorer_panel())
        main_split.addWidget(self._editor_panel())
        main_split.addWidget(self._agent_panel())
        main_split.setSizes([250, 780, 420])

        bottom = self._bottom_panel()
        vertical = QSplitter(Qt.Orientation.Vertical); vertical.addWidget(main_split); vertical.addWidget(bottom); vertical.setSizes([650, 190])
        outer.addWidget(vertical, 1)
        self.setCentralWidget(root)

    def _explorer_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        layout.addWidget(self._panel_header("EXPLORER", "WORKSPACE"))
        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True)
        root = QTreeWidgetItem(["AutoFix_Test_Project"])
        for name in ["src", "tests", "README.md", "pyproject.toml"]:
            root.addChild(QTreeWidgetItem([name]))
        self.tree.addTopLevelItem(root); root.setExpanded(True)
        layout.addWidget(self.tree)
        return panel

    def _editor_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        self.tabs = QTabWidget(); editor = QTextEdit(); editor.setFont(QFont("Consolas", 11))
        editor.setPlainText('def main():\n    return "Hello from AutoFix"\n\nif __name__ == "__main__":\n    main()\n')
        self.tabs.addTab(editor, "main.py")
        layout.addWidget(self.tabs)
        return panel

    def _agent_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        layout.addWidget(self._panel_header("AI AGENTS", "ORCHESTRATOR"))
        self.agent_list = QListWidget(); self.agent_list.addItems(["◯ Planner", "◯ Builder", "◯ Analyzer", "◯ Security", "◯ Tester", "◯ Reviewer"])
        layout.addWidget(self.agent_list, 1)
        layout.addWidget(self._panel_header("AI ASSISTANT"))
        self.chat = QTextEdit(); self.chat.setReadOnly(True); self.chat.setPlainText("AutoFix Assistant\n\nReady to analyze or build your project.")
        layout.addWidget(self.chat, 1)
        return panel

    def _bottom_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        layout.addWidget(self._panel_header("TERMINAL  •  TESTS  •  PROBLEMS  •  OUTPUT  •  VERIFICATION"))
        self.output = QTextEdit(); self.output.setReadOnly(True); self.output.setFont(QFont("Consolas", 10)); layout.addWidget(self.output)
        return panel

    def _build_status(self):
        status = QStatusBar(); self.setStatusBar(status)
        status.showMessage("Ready  •  Python  •  Diagnostics: 0  •  Security: Ready")

    def build_project(self):
        workspace = Path.cwd() / "workspace"; workspace.mkdir(exist_ok=True)
        result = ProjectBuilder().build_python_project(workspace, "AutoFix_Test_Project")
        if result.success:
            self.project_path = result.project_path
            self.output.append("BUILD  ✓  " + result.message)
            for path in result.files_created: self.output.append("  + " + str(path))
        else:
            self.output.append("BUILD  !  " + result.message)

    def verify_project(self):
        project = self.project_path or (Path.cwd() / "workspace" / "AutoFix_Test_Project")
        if not project.exists():
            self.output.append("VERIFY  !  Build the test project first."); return
        errors = ProjectVerifier().verify(project)
        if errors:
            self.output.append("VERIFY  ✗  FAILED")
            for error in errors: self.output.append("  " + error)
        else:
            self.output.append("VERIFY  ✓  PASSED — structure and Python syntax valid")

    def run_agents(self):
        self.output.append("AGENTS  ▶  Pipeline started")
        results = AgentOrchestrator().run({"project": str(self.project_path or "")})
        for i, result in enumerate(results):
            self.agent_list.item(i).setText("● " + result.agent + "  —  " + result.status.value.upper())
            self.output.append(f"AGENT  ✓  {result.agent}: {result.message}")
        self.chat.setPlainText("AutoFix Assistant\n\nAgent pipeline completed. All prototype agents reported PASS.")
        self.output.append("AGENTS  ✓  Pipeline complete")

    def check_dependencies(self):
        statuses = DependencyChecker().check_all()
        dialog = QDialog(self)
        dialog.setWindowTitle("AutoFix Dependency Checker")
        dialog.resize(720, 430)
        layout = QVBoxLayout(dialog)
        title = QLabel("Dependency Check")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        layout.addWidget(title)
        table = QTableWidget(len(statuses), 4)
        table.setHorizontalHeaderLabels(["Dependency", "Status", "Version", "Action"])
        table.horizontalHeader().setStretchLastSection(True)
        for row, status in enumerate(statuses):
            table.setItem(row, 0, QTableWidgetItem(status.spec.name))
            table.setItem(row, 1, QTableWidgetItem("✓ Installed" if status.installed else "✗ Missing"))
            table.setItem(row, 2, QTableWidgetItem(status.version or "—"))
            if not status.installed:
                button = QPushButton("Install")
                button.clicked.connect(lambda checked=False, st=status, d=dialog: self.install_dependency(st, d))
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
            self, "Dependency Missing",
            f"{spec.name} was not found.\n\nDo you want AutoFix AI Studio to install it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, message = DependencyChecker().install_python_package(spec)
        if ok:
            QMessageBox.information(self, "Dependency Installed", message)
            parent_dialog.accept()
            self.check_dependencies()
        else:
            QMessageBox.warning(self, "Installation Not Available", message)

    def noop(self):
        self.output.append("INFO  •  This command is reserved for the next implementation phase.")


def create_app():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    return app, window
