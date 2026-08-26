from pathlib import Path
import shlex

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QGridLayout,
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
    QFrame,
    QFileDialog,
)

from app.api.client import BackendClient, ApiError


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("AutoFix AI Studio")
        self.resize(1200, 780)
        self.setMinimumSize(1000, 650)

        self.client = BackendClient()

        self.current_job_id = None
        self.job_submitting = False
        self.last_job_status = None

        self.apply_dark_theme()
        self.build_ui()

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_job)

        self.check_backend()

    # =========================================================
    # Theme
    # =========================================================

    def apply_dark_theme(self):
        theme_path = (
            Path(__file__).resolve().parent
            / "dark_theme.qss"
        )

        try:
            stylesheet = theme_path.read_text(
                encoding="utf-8"
            )

            self.setStyleSheet(stylesheet)

            print(
                f"[AutoFix] Dark theme loaded: {theme_path}"
            )

        except Exception as exc:
            print(
                f"[AutoFix] Failed to load dark theme: {exc}"
            )

    # =========================================================
    # Main UI
    # =========================================================

    def build_ui(self):
        self.nav = QListWidget()
        self.nav.setObjectName("sidebar")
        self.nav.setFixedWidth(215)

        self.nav.addItems([
            "Dashboard",
            "New Job",
            "Job Monitor",
            "Settings",
        ])

        self.pages = QStackedWidget()
        self.pages.setObjectName("pages")

        self.pages.addWidget(self.dashboard_page())
        self.pages.addWidget(self.new_job_page())
        self.pages.addWidget(self.monitor_page())
        self.pages.addWidget(self.settings_page())

        self.nav.currentRowChanged.connect(
            self.pages.setCurrentIndex
        )

        self.nav.setCurrentRow(0)

        central = QWidget()
        central.setObjectName("central")

        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self.nav)
        layout.addWidget(self.pages, 1)

        self.setCentralWidget(central)

    # =========================================================
    # Helpers
    # =========================================================

    def title(self, text, subtitle=None):
        container = QWidget()

        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(4)

        label = QLabel(text)
        label.setObjectName("title")

        layout.addWidget(label)

        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("subtitle")
            layout.addWidget(sub)

        return container

    def card(self, title, value="-", status=None):
        frame = QFrame()
        frame.setObjectName("dashboardCard")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        heading = QLabel(title.upper())
        heading.setObjectName("cardHeading")

        value_label = QLabel(value)
        value_label.setObjectName("cardValue")

        layout.addWidget(heading)
        layout.addWidget(value_label)

        if status:
            status_label = QLabel(status)
            status_label.setObjectName("cardStatus")
            layout.addWidget(status_label)

        return frame

    # =========================================================
    # Dashboard
    # =========================================================

    def dashboard_page(self):
        page = QWidget()

        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 28, 30, 28)
        layout.setSpacing(18)

        layout.addWidget(
            self.title(
                "Dashboard",
                "AutoFix AI Studio control center",
            )
        )

        cards = QGridLayout()
        cards.setSpacing(14)

        backend_card = self.card(
            "Backend",
            "CHECKING",
            "Connection status",
        )

        provider_card = self.card(
            "AI Provider",
            "-",
            "Active provider",
        )

        workflow_card = self.card(
            "Workflow",
            "READY",
            "Automation pipeline",
        )

        self.dashboard_backend_value = (
            backend_card.findChild(
                QLabel,
                "cardValue",
            )
        )

        self.dashboard_provider_value = (
            provider_card.findChild(
                QLabel,
                "cardValue",
            )
        )

        self.dashboard_workflow_value = (
            workflow_card.findChild(
                QLabel,
                "cardValue",
            )
        )

        cards.addWidget(backend_card, 0, 0)
        cards.addWidget(provider_card, 0, 1)
        cards.addWidget(workflow_card, 0, 2)

        layout.addLayout(cards)

        hero = QFrame()
        hero.setObjectName("heroCard")

        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(
            24,
            24,
            24,
            24,
        )
        hero_layout.setSpacing(10)

        hero_title = QLabel(
            "Autonomous Code Repair"
        )
        hero_title.setObjectName("heroTitle")

        hero_text = QLabel(
            "Create a job and let AutoFix inspect, "
            "test, repair, and verify your project."
        )
        hero_text.setObjectName("heroText")
        hero_text.setWordWrap(True)

        start = QPushButton(
            "START NEW AUTOFIX JOB"
        )
        start.setObjectName("primaryButton")
        start.setMinimumHeight(42)

        start.clicked.connect(
            lambda: self.nav.setCurrentRow(1)
        )

        hero_layout.addWidget(hero_title)
        hero_layout.addWidget(hero_text)
        hero_layout.addSpacing(8)
        hero_layout.addWidget(start)

        layout.addWidget(hero)

        recent = QFrame()
        recent.setObjectName("sectionCard")

        recent_layout = QVBoxLayout(recent)
        recent_layout.setContentsMargins(
            20,
            18,
            20,
            18,
        )

        recent_title = QLabel("CURRENT JOB")
        recent_title.setObjectName("sectionTitle")

        self.dashboard_job_label = QLabel(
            "No job running."
        )
        self.dashboard_job_label.setObjectName(
            "sectionValue"
        )

        recent_layout.addWidget(recent_title)
        recent_layout.addWidget(
            self.dashboard_job_label
        )

        layout.addWidget(recent)

        refresh = QPushButton(
            "Refresh Backend Status"
        )

        refresh.clicked.connect(
            self.check_backend
        )

        layout.addWidget(refresh)
        layout.addStretch()

        return page

    # =========================================================
    # New Job
    # =========================================================

    def new_job_page(self):
        page = QWidget()

        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            30,
            28,
            30,
            28,
        )
        layout.setSpacing(16)

        layout.addWidget(
            self.title(
                "New Job",
                "Create an autonomous repair task",
            )
        )

        form_card = QFrame()
        form_card.setObjectName("sectionCard")

        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(
            22,
            22,
            22,
            22,
        )
        form_layout.setSpacing(16)

        form = QFormLayout()
        form.setSpacing(14)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
        )

        self.name_input = QLineEdit(
            "frontend-test"
        )

        self.name_input.setPlaceholderText(
            "Example: fix-login-tests"
        )

        self.task_input = QPlainTextEdit(
            "Create a demo Python add function "
            "and make its tests pass"
        )

        self.task_input.setPlaceholderText(
            "Describe what AutoFix should do..."
        )

        self.task_input.setMinimumHeight(150)

        workspace_row = QWidget()

        workspace_layout = QHBoxLayout(
            workspace_row
        )

        workspace_layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        workspace_layout.setSpacing(8)

        self.workspace_input = QLineEdit()

        self.workspace_input.setPlaceholderText(
            "Optional project workspace"
        )

        browse = QPushButton("Browse")
        browse.setObjectName(
            "secondaryButton"
        )

        browse.clicked.connect(
            self.browse_workspace
        )

        workspace_layout.addWidget(
            self.workspace_input,
            1,
        )

        workspace_layout.addWidget(
            browse
        )

        self.command_input = QLineEdit(
            "python -m pytest -q"
        )

        self.command_input.setPlaceholderText(
            "Example: python -m pytest -q"
        )

        self.command_presets = QComboBox()

        self.command_presets.addItems([
            "Custom",
            "Python / pytest",
            "Python / unittest",
            "Node / npm test",
            "Node / vitest",
        ])

        self.command_presets.currentIndexChanged.connect(
            self.apply_command_preset
        )

        command_row = QWidget()

        command_layout = QVBoxLayout(
            command_row
        )

        command_layout.setContentsMargins(
            0,
            0,
            0,
            0,
        )

        command_layout.setSpacing(7)

        command_layout.addWidget(
            self.command_input
        )

        command_layout.addWidget(
            self.command_presets
        )

        self.attempts_input = QSpinBox()
        self.attempts_input.setRange(0, 10)
        self.attempts_input.setValue(2)

        form.addRow(
            "Project Name",
            self.name_input,
        )

        form.addRow(
            "Task",
            self.task_input,
        )

        form.addRow(
            "Workspace",
            workspace_row,
        )

        form.addRow(
            "Test Command",
            command_row,
        )

        form.addRow(
            "Max Attempts",
            self.attempts_input,
        )

        form_layout.addLayout(form)

        hint = QLabel(
            "AutoFix will execute the configured test command "
            "and use the available repair workflow when tests fail."
        )

        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        form_layout.addWidget(hint)

        layout.addWidget(form_card)

        self.start_button = QPushButton(
            "START AUTOFIX"
        )

        self.start_button.setObjectName(
            "primaryButton"
        )

        self.start_button.setMinimumHeight(46)

        self.start_button.clicked.connect(
            self.create_job
        )

        layout.addWidget(
            self.start_button
        )

        layout.addStretch()

        return page

    def browse_workspace(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Project Workspace",
            self.workspace_input.text().strip()
            or str(Path.home()),
        )

        if directory:
            self.workspace_input.setText(
                directory
            )

    def apply_command_preset(self, index):
        presets = {
            1: "python -m pytest -q",
            2: "python -m unittest discover -v",
            3: "npm test",
            4: "npx vitest run",
        }

        if index in presets:
            self.command_input.setText(
                presets[index]
            )

    # =========================================================
    # Job Monitor v2
    # =========================================================

    def monitor_page(self):
        page = QWidget()
        page.setObjectName("monitorPage")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 28, 30, 24)
        layout.setSpacing(14)

        layout.addWidget(
            self.title(
                "Job Monitor",
                "Live AutoFix execution monitor",
            )
        )

        # Job header
        header = QFrame()
        header.setObjectName("sectionCard")

        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 16)
        header_layout.setSpacing(5)

        self.job_label = QLabel("No job selected.")
        self.job_label.setObjectName("sectionTitle")

        self.status_label = QLabel("STATUS: IDLE")
        self.status_label.setObjectName("statusRunning")

        header_layout.addWidget(self.job_label)
        header_layout.addWidget(self.status_label)

        layout.addWidget(header)

        # Lifecycle
        lifecycle = QFrame()
        lifecycle.setObjectName("sectionCard")

        lifecycle_layout = QHBoxLayout(lifecycle)
        lifecycle_layout.setContentsMargins(14, 14, 14, 14)
        lifecycle_layout.setSpacing(6)

        self.phase_labels = []

        phases = [
            ("1", "CREATED"),
            ("2", "ANALYZING"),
            ("3", "TESTING"),
            ("4", "REPAIRING"),
            ("5", "VERIFYING"),
        ]

        for index, (number, name) in enumerate(phases):
            phase = QFrame()
            phase.setObjectName("phaseCard")
            phase.setMinimumHeight(76)
            phase.setMaximumHeight(76)

            phase_layout = QVBoxLayout(phase)
            phase_layout.setContentsMargins(8, 8, 8, 8)
            phase_layout.setSpacing(4)

            number_label = QLabel(number)
            number_label.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            number_label.setObjectName("phaseNumber")

            name_label = QLabel(name)
            name_label.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            name_label.setObjectName("phaseLabel")

            phase_layout.addWidget(number_label)
            phase_layout.addWidget(name_label)

            lifecycle_layout.addWidget(phase, 1)
            self.phase_labels.append(phase)

            if index < len(phases) - 1:
                separator = QFrame()
                separator.setObjectName("phaseSeparator")
                separator.setFrameShape(
                    QFrame.Shape.VLine
                )
                separator.setFrameShadow(
                    QFrame.Shadow.Plain
                )
                separator.setFixedWidth(2)

                lifecycle_layout.addWidget(
                    separator
                )

                arrow = QLabel("›")
                arrow.setAlignment(
                    Qt.AlignmentFlag.AlignCenter
                )
                arrow.setObjectName("phaseArrow")
                arrow.setFixedWidth(18)

                lifecycle_layout.addWidget(
                    arrow
                )

        layout.addWidget(lifecycle)

        # Statistics
        stats = QHBoxLayout()
        stats.setSpacing(0)

        attempts_card, self.monitor_attempts_value = (
            self.create_monitor_stat(
                "ATTEMPTS",
                "0 / 0",
            )
        )

        status_card, self.monitor_status_value = (
            self.create_monitor_stat(
                "STATUS",
                "IDLE",
            )
        )

        refresh_card, self.monitor_refresh_value = (
            self.create_monitor_stat(
                "AUTO REFRESH",
                "ON",
            )
        )

        stats.addWidget(attempts_card, 1)
        stats.addWidget(status_card, 1)
        stats.addWidget(refresh_card, 1)

        layout.addLayout(stats)

        # Event header
        event_header = QHBoxLayout()
        event_header.setContentsMargins(2, 4, 2, 0)

        event_title = QLabel("LIVE EVENT LOG")
        event_title.setObjectName("sectionTitle")

        self.event_count_label = QLabel("0 events")
        self.event_count_label.setObjectName("hintLabel")

        event_header.addWidget(event_title)
        event_header.addStretch()
        event_header.addWidget(self.event_count_label)

        layout.addLayout(event_header)

        # Event console
        event_frame = QFrame()
        event_frame.setObjectName("eventConsoleFrame")

        event_frame_layout = QVBoxLayout(
            event_frame
        )
        event_frame_layout.setContentsMargins(
            1, 1, 1, 1
        )

        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setObjectName("eventConsole")

        event_frame_layout.addWidget(
            self.events
        )
        self.events.setPlaceholderText(
            "Waiting for AutoFix events..."
        )

        layout.addWidget(event_frame, 1)

        # Bottom controls
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 2, 0, 0)

        self.monitor_refresh_button = QPushButton(
            "REFRESH NOW"
        )

        self.monitor_refresh_button.clicked.connect(
            self.refresh_job
        )

        controls.addWidget(
            self.monitor_refresh_button
        )

        controls.addStretch()

        self.monitor_timer_label = QLabel(
            "Auto-refresh: ON"
        )
        self.monitor_timer_label.setObjectName(
            "hintLabel"
        )

        controls.addWidget(
            self.monitor_timer_label
        )

        layout.addLayout(controls)

        return page

    def create_monitor_stat(self, title, value):
        frame = QFrame()
        frame.setObjectName("dashboardCard")
        frame.setMinimumHeight(82)
        frame.setMaximumHeight(82)

        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 11, 16, 11)
        box.setSpacing(3)

        heading = QLabel(title)
        heading.setObjectName("cardHeading")

        value_label = QLabel(value)
        value_label.setObjectName("cardValue")

        box.addWidget(heading)
        box.addWidget(value_label)

        return frame, value_label

    # =========================================================
    # Settings
    # =========================================================

    def settings_page(self):
        page = QWidget()

        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            30,
            28,
            30,
            28,
        )
        layout.setSpacing(16)

        layout.addWidget(
            self.title(
                "Settings",
                "Configure AutoFix connection and provider",
            )
        )

        card = QFrame()

        card.setObjectName(
            "sectionCard"
        )

        card_layout = QVBoxLayout(card)

        card_layout.setContentsMargins(
            22,
            22,
            22,
            22,
        )

        form = QFormLayout()

        form.setSpacing(14)

        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight
        )

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
            self.backend_url,
        )

        form.addRow(
            "Provider",
            self.provider_combo,
        )

        card_layout.addLayout(
            form
        )

        layout.addWidget(
            card
        )

        save = QPushButton(
            "APPLY SETTINGS"
        )

        save.clicked.connect(
            self.apply_settings
        )

        layout.addWidget(
            save
        )

        layout.addStretch()

        return page

    # =========================================================
    # Backend
    # =========================================================

    def apply_settings(self):
        url = (
            self.backend_url.text()
            .strip()
            .rstrip("/")
        )

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

            health_status = (
                health.get(
                    "status",
                    "unknown",
                )
                .upper()
            )

            provider = status.get(
                "provider",
                "-",
            )

            workflow = status.get(
                "workflow",
                [],
            )

            workflow_text = (
                "READY"
                if workflow
                else "IDLE"
            )

            self.dashboard_backend_value.setText(
                health_status
            )

            self.dashboard_provider_value.setText(
                provider
            )

            self.dashboard_workflow_value.setText(
                workflow_text
            )

            if self.current_job_id:
                self.dashboard_job_label.setText(
                    f"Job ID: {self.current_job_id}"
                )

            else:
                self.dashboard_job_label.setText(
                    "No job running."
                )

        except ApiError as exc:
            self.dashboard_backend_value.setText(
                "OFFLINE"
            )

            self.dashboard_provider_value.setText(
                "-"
            )

            self.dashboard_workflow_value.setText(
                "UNAVAILABLE"
            )

            self.dashboard_job_label.setText(
                f"Backend unavailable: {exc.message}"
            )

    # =========================================================
    # Create Job
    # =========================================================

    def create_job(self):
        if self.job_submitting:
            return

        name = (
            self.name_input.text()
            .strip()
        )

        task = (
            self.task_input.toPlainText()
            .strip()
        )

        workspace = (
            self.workspace_input.text()
            .strip()
            or None
        )

        command_text = (
            self.command_input.text()
            .strip()
        )

        if not name:
            QMessageBox.warning(
                self,
                "Validation",
                "Project name is required.",
            )
            self.name_input.setFocus()
            return

        if not task:
            QMessageBox.warning(
                self,
                "Validation",
                "Task description is required.",
            )
            self.task_input.setFocus()
            return

        if not command_text:
            QMessageBox.warning(
                self,
                "Validation",
                "Test command is required.",
            )
            self.command_input.setFocus()
            return

        if workspace:
            workspace_path = Path(
                workspace
            )

            if not workspace_path.exists():
                QMessageBox.warning(
                    self,
                    "Invalid Workspace",
                    "The selected workspace does not exist.",
                )
                self.workspace_input.setFocus()
                return

            if not workspace_path.is_dir():
                QMessageBox.warning(
                    self,
                    "Invalid Workspace",
                    "Workspace must be a directory.",
                )
                self.workspace_input.setFocus()
                return

        try:
            command = shlex.split(
                command_text
            )

        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Invalid Test Command",
                str(exc),
            )
            self.command_input.setFocus()
            return

        if not command:
            QMessageBox.warning(
                self,
                "Validation",
                "Test command is required.",
            )
            return

        payload = {
            "name": name,
            "task": task,
            "workspace": workspace,
            "max_attempts": (
                self.attempts_input.value()
            ),
            "command": command,
        }

        self.job_submitting = True

        self.start_button.setEnabled(
            False
        )

        self.start_button.setText(
            "CREATING JOB..."
        )

        try:
            result = (
                self.client.create_job(
                    payload
                )
            )

            self.current_job_id = (
                result["job_id"]
            )

            self.last_job_status = None

            self.dashboard_job_label.setText(
                f"{name} — STARTING"
            )

            self.nav.setCurrentRow(
                2
            )

            self.timer.start()

            self.refresh_job()

        except ApiError as exc:
            QMessageBox.critical(
                self,
                "Backend Error",
                exc.message,
            )

        finally:
            self.job_submitting = False

            self.start_button.setEnabled(
                True
            )

            self.start_button.setText(
                "START AUTOFIX"
            )

    # =========================================================
    # Job Monitor Refresh
    # =========================================================

    def refresh_job(self):
        if not self.current_job_id:
            self.monitor_status_value.setText(
                "IDLE"
            )

            self.monitor_attempts_value.setText(
                "0 / 0"
            )

            self.monitor_timer_label.setText(
                "Auto-refresh: OFF"
            )

            return

        try:
            job = self.client.job(
                self.current_job_id
            )

            events = self.client.events(
                self.current_job_id
            )

            job_name = job.get(
                "name",
                "Unknown",
            )

            job_id = job.get(
                "job_id",
                self.current_job_id,
            )

            status = (
                job.get(
                    "status",
                    "unknown",
                )
                .lower()
            )

            attempts = job.get(
                "attempts",
                0,
            )

            max_attempts = (
                job.get(
                    "max_attempts",
                    self.attempts_input.value(),
                )
            )

            message = job.get(
                "message",
                "",
            )

            # -------------------------------------------------
            # Header
            # -------------------------------------------------

            self.job_label.setText(
                f"{job_name}    |    ID: {job_id}"
            )

            self.status_label.setText(
                f"STATUS: {status.upper()}   |   "
                f"ATTEMPTS: {attempts} / {max_attempts}   |   "
                f"{message}"
            )

            # -------------------------------------------------
            # Status styling
            # -------------------------------------------------

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

            # -------------------------------------------------
            # Stats
            # -------------------------------------------------

            self.monitor_attempts_value.setText(
                f"{attempts} / {max_attempts}"
            )

            self.monitor_status_value.setText(
                status.upper()
            )

            self.monitor_refresh_value.setText(
                "ON"
                if self.timer.isActive()
                else "OFF"
            )

            # -------------------------------------------------
            # Lifecycle
            # -------------------------------------------------

            self.update_lifecycle(
                status
            )

            # -------------------------------------------------
            # Events
            # -------------------------------------------------

            event_items = events.get(
                "events",
                [],
            )

            lines = []

            for event in event_items:
                sequence = event.get(
                    "sequence",
                    0,
                )

                event_name = event.get(
                    "event",
                    "event",
                )

                event_message = event.get(
                    "message",
                    "",
                )

                lines.append(
                    f"[{sequence:02d}] "
                    f"{event_name.upper():10s} "
                    f"{event_message}"
                )

            self.events.setPlainText(
                "\n".join(lines)
            )

            self.event_count_label.setText(
                f"{len(event_items)} events"
            )

            # -------------------------------------------------
            # Dashboard
            # -------------------------------------------------

            self.dashboard_job_label.setText(
                f"{job_name} — "
                f"{status.upper()}"
            )

            # -------------------------------------------------
            # Completion
            # -------------------------------------------------

            if status in (
                "verified",
                "failed",
            ):
                self.timer.stop()

                self.monitor_timer_label.setText(
                    "Auto-refresh: OFF"
                )

                self.monitor_refresh_value.setText(
                    "OFF"
                )

            else:
                if not self.timer.isActive():
                    self.timer.start()

                self.monitor_timer_label.setText(
                    "Auto-refresh: ON"
                )

                self.monitor_refresh_value.setText(
                    "ON"
                )

            self.last_job_status = status

        except ApiError as exc:
            self.status_label.setText(
                f"MONITOR ERROR: {exc.message}"
            )

            self.monitor_status_value.setText(
                "ERROR"
            )

            self.monitor_timer_label.setText(
                "Auto-refresh: ON"
            )
