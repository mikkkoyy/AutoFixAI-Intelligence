from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QListWidget,
    QLabel,
    QPushButton,
    QStackedWidget,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QMessageBox,
    QComboBox,
)

from app.api.client import BackendClient, ApiError


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("AutoFix AI Studio")
        self.resize(1180, 760)

        self.apply_dark_theme()

        self.client = BackendClient()
        self.current_job_id = None

        self.nav = QListWidget()
        self.nav.addItems([
            "Dashboard",
            "New Job",
            "Job Monitor",
            "Settings",
        ])
        self.nav.setFixedWidth(210)

        self.pages = QStackedWidget()

        self.pages.addWidget(self.dashboard_page())
        self.pages.addWidget(self.new_job_page())
        self.pages.addWidget(self.monitor_page())
        self.pages.addWidget(self.settings_page())

        self.nav.currentRowChanged.connect(
            self.pages.setCurrentIndex
        )

        self.nav.setCurrentRow(0)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self.nav)
        layout.addWidget(self.pages, 1)

        self.setCentralWidget(central)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_job)

        self.check_backend()

    def apply_dark_theme(self):
        theme_path = Path(__file__).resolve().parent / "dark_theme.qss"

        try:
            stylesheet = theme_path.read_text(encoding="utf-8")
            self.setStyleSheet(stylesheet)
            print(f"[AutoFix] Dark theme loaded: {theme_path}")
        except Exception as exc:
            print(f"[AutoFix] Failed to load dark theme: {exc}")

    def title(self, text):
        label = QLabel(text)
        label.setObjectName("title")
        return label

    def dashboard_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        layout.addWidget(self.title("Dashboard"))

        self.backend_label = QLabel("Backend: checking...")
        self.provider_label = QLabel("Provider: -")
        self.workflow_label = QLabel("Workflow: -")

        self.backend_label.setObjectName("statusOnline")
        self.provider_label.setObjectName("infoLabel")
        self.workflow_label.setObjectName("infoLabel")

        layout.addWidget(self.backend_label)
        layout.addWidget(self.provider_label)
        layout.addWidget(self.workflow_label)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.check_backend)
        layout.addWidget(refresh)

        layout.addStretch()

        return page

    def new_job_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        layout.addWidget(self.title("New Job"))

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(
            __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.AlignRight
        )

        self.name_input = QLineEdit("frontend-test")

        self.task_input = QPlainTextEdit(
            "Create a demo Python add function and make its tests pass"
        )
        self.task_input.setMinimumHeight(120)

        self.workspace_input = QLineEdit()

        self.command_input = QLineEdit(
            "python -m pytest -q"
        )

        self.attempts_input = QSpinBox()
        self.attempts_input.setRange(0, 10)
        self.attempts_input.setValue(2)

        form.addRow("Project Name", self.name_input)
        form.addRow("Task", self.task_input)
        form.addRow("Workspace", self.workspace_input)
        form.addRow("Test Command", self.command_input)
        form.addRow("Max Attempts", self.attempts_input)

        layout.addLayout(form)

        start = QPushButton("START AUTOFIX")
        start.setObjectName("primaryButton")
        start.clicked.connect(self.create_job)

        layout.addWidget(start)
        layout.addStretch()

        return page

    def monitor_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        layout.addWidget(self.title("Job Monitor"))

        self.job_label = QLabel("No job selected.")
        self.job_label.setObjectName("jobLabel")

        self.status_label = QLabel("Status: -")
        self.status_label.setObjectName("statusRunning")

        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)

        layout.addWidget(self.job_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.events, 1)

        return page

    def settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        layout.addWidget(self.title("Settings"))

        form = QFormLayout()
        form.setSpacing(12)

        self.backend_url = QLineEdit(
            self.client.base_url
        )

        self.provider_combo = QComboBox()
        self.provider_combo.addItems([
            "deterministic",
            "openai-compatible",
        ])

        form.addRow(
            "Backend URL",
            self.backend_url
        )

        form.addRow(
            "Provider",
            self.provider_combo
        )

        layout.addLayout(form)

        save = QPushButton("Apply Settings")
        save.clicked.connect(self.apply_settings)

        layout.addWidget(save)
        layout.addStretch()

        return page

    def apply_settings(self):
        url = self.backend_url.text().strip().rstrip("/")

        if not url:
            QMessageBox.warning(
                self,
                "Settings",
                "Backend URL is required.",
            )
            return

        self.client.base_url = url
        self.check_backend()

    def check_backend(self):
        try:
            health = self.client.health()
            status = self.client.status()

            self.backend_label.setText(
                f"Backend: {health.get('status', 'unknown').upper()}"
            )

            self.backend_label.setObjectName("statusOnline")
            self.backend_label.style().unpolish(
                self.backend_label
            )
            self.backend_label.style().polish(
                self.backend_label
            )

            provider = status.get("provider", "-")

            self.provider_label.setText(
                f"Provider: {provider}"
            )

            workflow = status.get("workflow", [])

            self.workflow_label.setText(
                "Workflow: " +
                " → ".join(workflow)
            )

        except ApiError as exc:
            self.backend_label.setText(
                "Backend: OFFLINE"
            )

            self.backend_label.setObjectName(
                "statusOffline"
            )

            self.backend_label.style().unpolish(
                self.backend_label
            )
            self.backend_label.style().polish(
                self.backend_label
            )

            self.provider_label.setText(
                "Provider: -"
            )

            self.workflow_label.setText(
                str(exc)
            )

    def create_job(self):
        import shlex

        command_text = (
            self.command_input.text().strip()
        )

        try:
            command = shlex.split(command_text)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Invalid Command",
                str(exc),
            )
            return

        payload = {
            "name": self.name_input.text().strip(),
            "task": self.task_input.toPlainText().strip(),
            "workspace": (
                self.workspace_input.text().strip()
                or None
            ),
            "max_attempts": (
                self.attempts_input.value()
            ),
            "command": command,
        }

        if not payload["name"]:
            QMessageBox.warning(
                self,
                "Validation",
                "Project name is required.",
            )
            return

        if not payload["task"]:
            QMessageBox.warning(
                self,
                "Validation",
                "Task is required.",
            )
            return

        if not command:
            QMessageBox.warning(
                self,
                "Validation",
                "Test command is required.",
            )
            return

        try:
            result = self.client.create_job(
                payload
            )

            self.current_job_id = result["job_id"]

            self.nav.setCurrentRow(2)

            self.timer.start(1000)

            self.refresh_job()

        except ApiError as exc:
            QMessageBox.critical(
                self,
                "Backend Error",
                exc.message,
            )

    def refresh_job(self):
        if not self.current_job_id:
            return

        try:
            job = self.client.job(
                self.current_job_id
            )

            events = self.client.events(
                self.current_job_id
            )

            self.job_label.setText(
                f"Job: {job['name']}    "
                f"ID: {job['job_id']}"
            )

            status = job["status"]

            self.status_label.setText(
                f"Status: {status.upper()} | "
                f"Attempts: {job['attempts']} | "
                f"{job['message']}"
            )

            if status == "verified":
                self.status_label.setObjectName(
                    "statusVerified"
                )

            elif status == "failed":
                self.status_label.setObjectName(
                    "statusFailed"
                )

            else:
                self.status_label.setObjectName(
                    "statusRunning"
                )

            self.status_label.style().unpolish(
                self.status_label
            )
            self.status_label.style().polish(
                self.status_label
            )

            lines = []

            for event in events.get("events", []):
                lines.append(
                    f"[{event['sequence']:02d}] "
                    f"{event['event'].upper():10s} "
                    f"{event['message']}"
                )

            self.events.setPlainText(
                "\n".join(lines)
            )

            if status in ("verified", "failed"):
                self.timer.stop()

        except ApiError as exc:
            self.status_label.setText(
                f"Monitor error: {exc.message}"
            )
