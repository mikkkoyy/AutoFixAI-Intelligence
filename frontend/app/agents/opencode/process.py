"""OpenCode process management — lifecycle, output streaming, stop/cancel."""

from PySide6.QtCore import QProcess, QObject, Signal


class OpenCodeProcess(QObject):
    """Manages a single OpenCode QProcess instance.

    Signals:
        output_received(str): Emitted when OpenCode produces stdout/stderr.
        process_finished(int): Emitted when the process exits.
        process_error(str): Emitted on error.
    """

    output_received = Signal(str)
    process_finished = Signal(int)
    process_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._executable: str = "opencode"

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.state() != QProcess.ProcessState.NotRunning
        )

    @property
    def executable(self) -> str:
        return self._executable

    def start(self, executable: str, cwd: str, args: list[str] | None = None):
        """Start OpenCode as a child process.

        Args:
            executable: Path to the opencode binary.
            cwd: Working directory (the Explorer workspace).
            args: Optional CLI arguments.
        """
        if self.is_running:
            self.stop()

        self._executable = executable
        self._process = QProcess(self)
        self._process.setWorkingDirectory(cwd)
        self._process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )

        self._process.readyRead.connect(self._on_ready_read)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error)

        start_args = args or []
        self._process.start(executable, start_args)

    def write(self, data: str):
        """Write data to the process stdin."""
        if self._process and self.is_running:
            self._process.write((data + "\r\n").encode("utf-8"))

    def stop(self):
        """Terminate the process gracefully, kill if needed."""
        if not self._process:
            return

        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(1500):
                self._process.kill()
                self._process.waitForFinished(1000)

    def cleanup(self):
        """Disconnect signals and release handles."""
        if self._process:
            try:
                self._process.readyRead.disconnect(self._on_ready_read)
                self._process.finished.disconnect(self._on_finished)
                self._process.errorOccurred.disconnect(self._on_error)
            except RuntimeError:
                pass

            self.stop()
            self._process.deleteLater()
            self._process = None

    def _on_ready_read(self):
        if self._process:
            data = self._process.readAll()
            if data:
                text = bytes(data).decode("utf-8", errors="replace")
                self.output_received.emit(text)

    def _on_finished(self, exit_code: int, _exit_status):
        self.process_finished.emit(exit_code)

    def _on_error(self, error):
        self.process_error.emit(str(error))
