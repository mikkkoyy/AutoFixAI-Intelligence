"""ConPTY-backed PTY transport for interactive TUI applications like OpenCode."""

import winpty as _winpty
from PySide6.QtCore import QObject, QTimer, Signal


class PTYTransport(QObject):
    """Manages a Windows ConPTY pseudo-terminal and streams I/O via Qt signals.

    Signals:
        output_received(str): Emitted when the PTY produces output.
        process_finished(int): Emitted when the child process exits.
        process_error(str): Emitted on a transport-level error.
    """

    output_received = Signal(str)
    process_finished = Signal(int)
    process_error = Signal(str)

    _POLL_MS = 16  # ~60 Hz polling

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pty: _winpty.PTY | None = None
        self._cols = 80
        self._rows = 24
        self._running = False
        self._spawned = False

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, executable: str, cwd: str, args: list[str] | None = None,
              cols: int = 80, rows: int = 24):
        """Create a PTY, spawn *executable* inside it, and begin polling."""
        if self._running:
            self.stop()

        self._cols = max(cols, 1)
        self._rows = max(rows, 1)

        try:
            self._pty = _winpty.PTY(self._cols, self._rows)
        except Exception as exc:
            self.process_error.emit(f"Failed to create PTY: {exc}")
            return

        cmdline = executable
        if args:
            cmdline = executable + " " + " ".join(args)

        try:
            self._pty.spawn(executable, cmdline=cmdline if args else None, cwd=cwd)
            self._spawned = True
        except Exception as exc:
            self.process_error.emit(f"Failed to spawn process: {exc}")
            self._cleanup_pty()
            return

        self._running = True
        self._poll_timer.start(self._POLL_MS)

    def write(self, data: str):
        """Write *data* to the PTY input stream."""
        if self._pty and self._running:
            try:
                self._pty.write(data)
            except Exception:
                pass

    def resize(self, cols: int, rows: int):
        """Resize the pseudo-terminal."""
        cols = max(cols, 1)
        rows = max(rows, 1)
        self._cols = cols
        self._rows = rows
        if self._pty and self._running:
            try:
                self._pty.set_size(cols, rows)
            except Exception:
                pass

    def stop(self):
        """Terminate the PTY and stop polling."""
        self._running = False
        self._poll_timer.stop()
        self._cleanup_pty()

    @property
    def is_alive(self) -> bool:
        if self._pty and self._spawned:
            try:
                return self._pty.isalive()
            except Exception:
                return False
        return False

    def get_exit_status(self) -> int | None:
        if self._pty and self._spawned:
            try:
                return self._pty.get_exitstatus()
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll(self):
        """Read all available output from the PTY (non-blocking)."""
        if not self._pty or not self._running:
            return

        chunks: list[str] = []
        try:
            while True:
                data = self._pty.read(blocking=False)
                if not data:
                    break
                chunks.append(data)
        except Exception:
            pass

        if chunks:
            self.output_received.emit("".join(chunks))

        if self._spawned and not self.is_alive:
            self._running = False
            self._poll_timer.stop()
            exit_code = self.get_exit_status() or 0
            self.process_finished.emit(exit_code)

    def _cleanup_pty(self):
        if self._pty is not None:
            try:
                self._pty.cancel_io()
            except Exception:
                pass
            self._pty = None
            self._spawned = False
