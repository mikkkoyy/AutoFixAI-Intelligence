"""Widget that renders a ConPTY-backed terminal with pyte screen emulation.

Provides a QWidget that accepts direct keyboard input, renders ANSI-colored
output via QPainter, and communicates with a PTYTransport for I/O.
"""

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import QApplication, QWidget

import pyte
import tempfile

from app.agents.opencode.pty_transport import PTYTransport

# ── Color map: pyte color name → QColor ────────────────────────
_COLOR_MAP: dict[str, QColor] = {
    "black": QColor("#1e1e1e"),
    "red": QColor("#cd3131"),
    "green": QColor("#00bc00"),
    "brown": QColor("#949800"),
    "yellow": QColor("#949800"),
    "blue": QColor("#0451a5"),
    "magenta": QColor("#bc05bc"),
    "cyan": QColor("#0598bc"),
    "white": QColor("#d6d6d6"),
    "brightblack": QColor("#666666"),
    "brightred": QColor("#cd3131"),
    "brightgreen": QColor("#14ce14"),
    "brightbrown": QColor("#b5ba00"),
    "brightyellow": QColor("#b5ba00"),
    "brightblue": QColor("#0451a5"),
    "brightmagenta": QColor("#bc05bc"),
    "brightcyan": QColor("#0598bc"),
    "brightwhite": QColor("#e5e5e5"),
}

_DEFAULT_FG = QColor("#e6eaf0")
_DEFAULT_BG = QColor("#0b0e13")
_CURSOR_COLOR = QColor("#e6eaf0")

_PASTE_LINE_THRESHOLD = 10
#: Above this many lines the payload is additionally staged to a temp file
#: (large-payload safety net) and delivered in smaller chunks.
_PASTE_FILE_THRESHOLD = 100
_PASTE_LARGE_CHUNK_SIZE = 64
_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"


def _resolve_color(name: str, default: QColor) -> QColor:
    if not name or name == "default":
        return default
    return _COLOR_MAP.get(name.lower(), default)


class OpenCodeTerminalWidget(QWidget):
    """A full interactive terminal surface backed by ConPTY + pyte.

    Keyboard events are forwarded directly to the PTY.  Output is fed through
    a ``pyte.Screen`` and rendered character-by-character via ``QPainter``.
    """

    process_started = Signal()
    process_finished = Signal(int)
    status_changed = Signal(str)

    _CURSOR_BLINK_MS = 530

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)

        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)

        fm = QFontMetrics(font)
        self._char_w = fm.horizontalAdvance("M")
        self._char_h = fm.height()

        self._cols = 80
        self._rows = 24
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.Stream(self._screen)

        self._transport = PTYTransport(self)
        self._transport.output_received.connect(self._on_output)
        self._transport.process_finished.connect(self._on_process_finished)
        self._transport.process_error.connect(self._on_process_error)

        self._cursor_visible = True
        self._cursor_blink = QTimer(self)
        self._cursor_blink.timeout.connect(self._toggle_cursor)
        self._cursor_blink.start(self._CURSOR_BLINK_MS)

        self._needs_full_repaint = True

        self._paste_chunk_size = 256
        self._active_chunk_size = self._paste_chunk_size
        self._pending_paste = ""
        self._pending_paste_offset = 0
        self._last_large_paste_file: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_process(self, executable: str, cwd: str, args: list[str] | None = None):
        self._recalc_size()
        self._transport.start(executable, cwd, args, self._cols, self._rows)
        self.status_changed.emit("Running")

    def stop_process(self):
        self._transport.stop()

    @property
    def transport(self) -> PTYTransport:
        return self._transport

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def sizeHint(self):
        return QSize(self._cols * self._char_w, self._rows * self._char_h)

    def minimumSizeHint(self):
        return QSize(self._char_w * 10, self._char_h * 4)

    def paintEvent(self, _event: QPaintEvent):
        painter = QPainter(self)
        painter.setFont(self.font())

        fm = painter.fontMetrics()
        char_w = fm.horizontalAdvance("M")
        char_h = fm.height()

        bg = _DEFAULT_BG
        painter.fillRect(self.rect(), bg)

        screen = self._screen
        cursor_x = screen.cursor.x
        cursor_y = screen.cursor.y

        for row in range(min(self._rows, screen.lines)):
            y = row * char_h
            line = screen.buffer.get(row)
            if line is None:
                continue
            for col in range(min(self._cols, screen.columns)):
                ch = line[col]
                x = col * char_w

                fg = _resolve_color(ch.fg, _DEFAULT_FG)
                cell_bg = _resolve_color(ch.bg, _DEFAULT_BG)

                if ch.reverse:
                    fg, cell_bg = cell_bg, fg

                painter.fillRect(x, y, char_w, char_h, cell_bg)

                if ch.bold:
                    fg = fg.lighter(130)

                painter.setPen(fg)

                display = ch.data or " "
                painter.drawText(x, y + fm.ascent(), display)

                if ch.underscore:
                    painter.setPen(fg)
                    painter.drawLine(x, y + char_h - 1, x + char_w, y + char_h - 1)

            if row == cursor_y and self._cursor_visible and self._transport.is_alive:
                cx = cursor_x * char_w
                painter.fillRect(cx, y, char_w, char_h, _CURSOR_COLOR)
                if cursor_x < self._cols and line is not None:
                    ch = line[cursor_x]
                    painter.setPen(cell_bg if not ch.reverse else _resolve_color(ch.fg, _DEFAULT_FG))
                    painter.drawText(cx, y + fm.ascent(), ch.data or " ")

        painter.end()

    def resizeEvent(self, event: QResizeEvent):
        super().resizeEvent(event)
        self._recalc_size()
        self._transport.resize(self._cols, self._rows)
        self.update()

    def keyPressEvent(self, event: QKeyEvent):
        if not self._transport.is_alive:
            super().keyPressEvent(event)
            return

        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)

        if ctrl and key in (Qt.Key.Key_V,):
            self._handle_paste()
            event.accept()
            return

        self._cursor_visible = True
        self._cursor_blink.start(self._CURSOR_BLINK_MS)

        seq = self._translate_key(event)
        if seq:
            self._transport.write(seq)
        event.accept()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._cursor_visible = True
        self._cursor_blink.start(self._CURSOR_BLINK_MS)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._cursor_visible = False
        self.update()

    # ------------------------------------------------------------------
    # Internal — paste handling
    # ------------------------------------------------------------------

    @staticmethod
    def _count_paste_lines(text: str) -> int:
        """Count lines in pasted text, normalizing CRLF/CR to LF first."""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.count("\n") + (1 if normalized and not normalized.endswith("\n") else 0)

    def _handle_paste(self):
        """Read clipboard and paste into the terminal.

        Tiers:
          * <= 10 lines   — normal paste.
          * 11–100 lines  — compact placeholder on screen, actual text sent
            with bracketed-paste markers, chunked asynchronously.
          * > 100 lines   — same as above plus a large-payload safety net:
            the payload is staged to a UTF-8 temp file (recoverable if the
            PTY chokes) and delivered in smaller chunks.

        All PTY writes are chunked and dispatched via QTimer so the Qt event
        loop (and therefore the PTY poll timer) stays alive throughout.  This
        prevents the synchronous winpty ``WriteFile`` call from blocking the
        GUI thread, which would deadlock OpenCode.
        """
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        text = clipboard.text()
        if not text:
            return

        line_count = self._count_paste_lines(text)
        chunk_size = self._paste_chunk_size

        if line_count > _PASTE_LINE_THRESHOLD:
            placeholder = f"[pasted ~{line_count} lines]"
            try:
                self._stream.feed(placeholder + "\r\n")
            except Exception:
                pass
            self.update()

            payload = _BRACKETED_PASTE_START + text + _BRACKETED_PASTE_END

            if line_count > _PASTE_FILE_THRESHOLD:
                self._last_large_paste_file = self._stage_large_payload(text)
                if self._last_large_paste_file:
                    try:
                        self._stream.feed(
                            f"[saved to {self._last_large_paste_file}]\r\n"
                        )
                    except Exception:
                        pass
                    self.update()
                chunk_size = _PASTE_LARGE_CHUNK_SIZE
        else:
            payload = text

        self._enqueue_paste(payload, chunk_size=chunk_size)

    @staticmethod
    def _stage_large_payload(text: str) -> str | None:
        """Stage a very large payload to a temp file; returns the path."""
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                prefix="autofix-paste-",
                encoding="utf-8",
                delete=False,
            )
            with handle:
                handle.write(text)
            return handle.name
        except Exception:
            return None

    def _enqueue_paste(self, text: str, chunk_size: int | None = None):
        """Queue *text* for async chunked delivery to the PTY."""
        if not self._transport.is_alive:
            return
        self._pending_paste = text
        self._pending_paste_offset = 0
        self._active_chunk_size = max(int(chunk_size or self._paste_chunk_size), 1)
        QTimer.singleShot(0, self._paste_next_chunk)

    def _paste_next_chunk(self):
        """Write the next chunk of a queued paste, then reschedule."""
        if not self._transport.is_alive:
            self._pending_paste = ""
            return
        if self._pending_paste_offset >= len(self._pending_paste):
            self._pending_paste = ""
            return
        end = min(self._pending_paste_offset + self._active_chunk_size,
                  len(self._pending_paste))
        chunk = self._pending_paste[self._pending_paste_offset:end]
        self._pending_paste_offset = end
        try:
            self._transport.write(chunk)
        except Exception:
            self._pending_paste = ""
            return
        QTimer.singleShot(0, self._paste_next_chunk)

    def contextMenuEvent(self, event: QContextMenuEvent):
        """Right-click → paste clipboard text with large-paste handling."""
        if not self._transport.is_alive:
            super().contextMenuEvent(event)
            return
        self._handle_paste()
        event.accept()

    # ------------------------------------------------------------------
    # Internal — rendering
    # ------------------------------------------------------------------

    def _recalc_size(self):
        fm = QFontMetrics(self.font())
        cw = fm.horizontalAdvance("M")
        ch = fm.height()
        if cw < 1 or ch < 1:
            return
        new_cols = max(self.width() // cw, 10)
        new_rows = max(self.height() // ch, 4)
        if new_cols != self._cols or new_rows != self._rows:
            self._cols = new_cols
            self._rows = new_rows
            old_screen = self._screen
            self._screen = pyte.Screen(self._cols, self._rows)
            self._stream = pyte.Stream(self._screen)
            self._stream.feed("".join(old_screen.display))

    # ------------------------------------------------------------------
    # Internal — PTY I/O
    # ------------------------------------------------------------------

    def _on_output(self, text: str):
        try:
            self._stream.feed(text)
        except Exception:
            pass
        self.update()

    def _on_process_finished(self, exit_code: int):
        self.status_changed.emit("Ready")
        self.process_finished.emit(exit_code)
        self.update()

    def _on_process_error(self, error: str):
        self.status_changed.emit("Error")
        self.update()

    def _toggle_cursor(self):
        self._cursor_visible = not self._cursor_visible
        if self._transport.is_alive:
            self.update()

    # ------------------------------------------------------------------
    # Internal — keyboard translation
    # ------------------------------------------------------------------

    def _translate_key(self, event: QKeyEvent) -> str | None:
        key = event.key()
        mods = event.modifiers()
        text = event.text()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        if ctrl:
            return self._ctrl_key(key, text)

        if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            return "\r"
        if key == Qt.Key.Key_Tab:
            return "\t"
        if key == Qt.Key.Key_Backspace:
            return "\x08"
        if key == Qt.Key.Key_Escape:
            return "\x1b"

        if key == Qt.Key.Key_Up:
            return "\x1b[A"
        if key == Qt.Key.Key_Down:
            return "\x1b[B"
        if key == Qt.Key.Key_Right:
            return "\x1b[C"
        if key == Qt.Key.Key_Left:
            return "\x1b[D"

        if key == Qt.Key.Key_Home:
            return "\x1b[H"
        if key == Qt.Key.Key_End:
            return "\x1b[F"

        if key == Qt.Key.Key_PageUp:
            return "\x1b[5~"
        if key == Qt.Key.Key_PageDown:
            return "\x1b[6~"

        if key == Qt.Key.Key_Insert:
            return "\x1b[2~"
        if key == Qt.Key.Key_Delete:
            return "\x1b[3~"

        if key == Qt.Key.Key_F1:
            return "\x1bOP"
        if key == Qt.Key.Key_F2:
            return "\x1bOQ"
        if key == Qt.Key.Key_F3:
            return "\x1bOR"
        if key == Qt.Key.Key_F4:
            return "\x1bOS"
        if key == Qt.Key.Key_F5:
            return "\x1b[15~"
        if key == Qt.Key.Key_F6:
            return "\x1b[17~"
        if key == Qt.Key.Key_F7:
            return "\x1b[18~"
        if key == Qt.Key.Key_F8:
            return "\x1b[19~"
        if key == Qt.Key.Key_F9:
            return "\x1b[20~"
        if key == Qt.Key.Key_F10:
            return "\x1b[21~"
        if key == Qt.Key.Key_F11:
            return "\x1b[23~"
        if key == Qt.Key.Key_F12:
            return "\x1b[24~"

        if text:
            return text

        return None

    def _ctrl_key(self, key: int, text: str) -> str | None:
        # Ctrl + letter → ASCII control character
        if text and len(text) == 1 and text.isalpha():
            return chr(ord(text.lower()) & 0x1F)

        # Ctrl+key combinations without readable text
        _CTRL_MAP = {
            Qt.Key.Key_Up: "\x1b[1;5A",
            Qt.Key.Key_Down: "\x1b[1;5B",
            Qt.Key.Key_Right: "\x1b[1;5C",
            Qt.Key.Key_Left: "\x1b[1;5D",
            Qt.Key.Key_Home: "\x1b[1;5H",
            Qt.Key.Key_End: "\x1b[1;5F",
            Qt.Key.Key_PageUp: "\x1b[5;5~",
            Qt.Key.Key_PageDown: "\x1b[6;5~",
            Qt.Key.Key_Insert: "\x1b[2;5~",
            Qt.Key.Key_Delete: "\x1b[3;5~",
            Qt.Key.Key_Backspace: "\x7f",
            Qt.Key.Key_Tab: "\x1b[Z",
        }
        return _CTRL_MAP.get(key)
