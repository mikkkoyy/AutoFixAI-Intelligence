"""Large-paste tier tests for OpenCodeTerminalWidget.

Tiers:
  * <= 10 lines  — normal paste, no placeholder.
  * 11–100 lines — placeholder + bracketed paste, chunked at default size.
  * > 100 lines  — placeholder + payload staged to a temp file, smaller chunks.

All delivery must go through the async chunk queue — never a synchronous
dump that could block the Qt event loop.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtCore import Qt

from app.ui.opencode_terminal_widget import (
    _BRACKETED_PASTE_END,
    _BRACKETED_PASTE_START,
    OpenCodeTerminalWidget,
)


class FakeTransport:
    """Stands in for PTYTransport; records every write."""

    def __init__(self):
        self.alive = True
        self.writes = []

    @property
    def is_alive(self):
        return self.alive

    def write(self, text):
        self.writes.append(text)


def make_widget(qapp, monkeypatch):
    widget = OpenCodeTerminalWidget()
    fake = FakeTransport()
    monkeypatch.setattr(widget, "_transport", fake, raising=True)
    return widget, fake


def drain(widget):
    """Run the queued paste to completion exactly like the event loop would."""
    for _ in range(10000):
        if not widget._pending_paste:
            break
        widget._paste_next_chunk()
    assert not widget._pending_paste, "paste queue did not drain"


def clipboard_lines(qapp, n):
    text = "\n".join(f"line-{i}" for i in range(n))
    qapp.clipboard().setText(text)
    return text


class TestSmallPaste:
    def test_small_paste_sends_text_verbatim(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        text = clipboard_lines(qapp, 3)

        widget._handle_paste()
        drain(widget)

        assert "".join(fake.writes) == text

    def test_small_paste_shows_no_placeholder(self, qapp, monkeypatch):
        widget, _fake = make_widget(qapp, monkeypatch)
        clipboard_lines(qapp, 5)

        widget._handle_paste()

        display = "\n".join(widget._screen.display)
        assert "[pasted" not in display


class TestMediumPaste:
    def test_eleven_lines_show_placeholder(self, qapp, monkeypatch):
        widget, _fake = make_widget(qapp, monkeypatch)
        clipboard_lines(qapp, 11)

        widget._handle_paste()

        display = "\n".join(widget._screen.display)
        assert "[pasted ~11 lines]" in display

    def test_ten_lines_stay_normal(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        text = clipboard_lines(qapp, 10)

        widget._handle_paste()
        drain(widget)

        assert "".join(fake.writes) == text
        assert "[pasted" not in "\n".join(widget._screen.display)

    def test_medium_payload_delivered_with_bracketed_markers(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        text = clipboard_lines(qapp, 50)

        widget._handle_paste()
        drain(widget)

        delivered = "".join(fake.writes)
        assert delivered.startswith(_BRACKETED_PASTE_START)
        assert delivered.endswith(_BRACKETED_PASTE_END)
        assert text in delivered

    def test_delivery_is_chunked(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        clipboard_lines(qapp, 50)

        widget._handle_paste()
        drain(widget)

        assert len(fake.writes) > 1, "payload must be written in chunks"


class TestVeryLargePaste:
    def test_over_hundred_lines_stage_temp_file(self, qapp, monkeypatch):
        widget, _fake = make_widget(qapp, monkeypatch)
        text = clipboard_lines(qapp, 145)

        widget._handle_paste()

        assert widget._last_large_paste_file, "large payload must be staged"
        with open(widget._last_large_paste_file, encoding="utf-8") as handle:
            assert handle.read() == text

    def test_over_hundred_lines_placeholder_and_note(self, qapp, monkeypatch):
        widget, _fake = make_widget(qapp, monkeypatch)
        clipboard_lines(qapp, 145)

        widget._handle_paste()

        display = "\n".join(widget._screen.display)
        assert "[pasted ~145 lines]" in display
        assert "[saved to" in display

    def test_large_payload_fully_delivered(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        text = clipboard_lines(qapp, 300)

        widget._handle_paste()
        drain(widget)

        delivered = "".join(fake.writes)
        assert delivered == _BRACKETED_PASTE_START + text + _BRACKETED_PASTE_END

    def test_large_payload_uses_smaller_chunks(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        clipboard_lines(qapp, 300)

        widget._handle_paste()
        drain(widget)

        assert max(len(w) for w in fake.writes) <= 64


class TestSafetyBehaviour:
    def test_dead_transport_aborts_queueing(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        fake.alive = False
        qapp.clipboard().setText("hello")

        widget._enqueue_paste("hello")

        assert widget._pending_paste == ""
        assert fake.writes == []

    def test_write_failure_clears_queue_without_raising(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)

        def boom(_text):
            raise OSError("pty gone")

        monkeypatch.setattr(fake, "write", boom)
        qapp.clipboard().setText("some content")

        widget._handle_paste()
        widget._paste_next_chunk()

        assert widget._pending_paste == ""

    def test_ctrl_v_routes_through_handle_paste(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        qapp.clipboard().setText("typed-body")

        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent

        event = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_V,
            Qt.KeyboardModifier.ControlModifier,
        )
        widget.keyPressEvent(event)

        drain(widget)
        assert "".join(fake.writes) == "typed-body"

    def test_right_click_routes_through_handle_paste(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        qapp.clipboard().setText("context-body")

        from PySide6.QtGui import QContextMenuEvent
        from PySide6.QtCore import QPoint

        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, QPoint(2, 2), QPoint(2, 2)
        )
        widget.contextMenuEvent(event)

        drain(widget)
        assert "".join(fake.writes) == "context-body"

    def test_empty_clipboard_is_noop(self, qapp, monkeypatch):
        widget, fake = make_widget(qapp, monkeypatch)
        qapp.clipboard().setText("")

        widget._handle_paste()

        assert fake.writes == []
        assert widget._pending_paste == ""
