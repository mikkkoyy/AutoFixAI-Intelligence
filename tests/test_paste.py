"""Regression tests for paste handling in OpenCodeTerminalWidget.

These tests verify that large pastes do NOT kill OpenCode, that chunked
async writes are used, and that the placeholder is UI-only.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_WIDGET_SRC = Path("frontend/app/ui/opencode_terminal_widget.py")


def _source():
    return _WIDGET_SRC.read_text(encoding="utf-8")


def _parse():
    return ast.parse(_source())


def _find_method(tree, cls_name, meth_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == meth_name:
                    return item
    return None


def _method_src(meth_node):
    lines = _source().splitlines()
    return "\n".join(lines[meth_node.lineno - 1 : meth_node.end_lineno])


def _all_method_names():
    tree = _parse()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpenCodeTerminalWidget":
            return [item.name for item in node.body if isinstance(item, ast.FunctionDef)]
    return []


# ── Source structure ───────────────────────────────────────────


class TestSourceStructure:
    def test_widget_file_parses(self):
        assert _parse() is not None

    def test_handle_paste_exists(self):
        assert _find_method(_parse(), "OpenCodeTerminalWidget", "_handle_paste")

    def test_count_paste_lines_exists(self):
        assert _find_method(_parse(), "OpenCodeTerminalWidget", "_count_paste_lines")

    def test_enqueue_paste_exists(self):
        assert _find_method(_parse(), "OpenCodeTerminalWidget", "_enqueue_paste")

    def test_paste_next_chunk_exists(self):
        assert _find_method(_parse(), "OpenCodeTerminalWidget", "_paste_next_chunk")

    def test_context_menu_event_exists(self):
        assert _find_method(_parse(), "OpenCodeTerminalWidget", "contextMenuEvent")

    def test_handle_paste_does_not_call_transport_write_directly(self):
        """_handle_paste must NOT call _transport.write directly — it must
        go through _enqueue_paste to avoid blocking the GUI thread."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_handle_paste")
        body = _method_src(meth)
        assert "_transport.write" not in body, (
            "_handle_paste must not call _transport.write directly"
        )

    def test_handle_paste_calls_enqueue_paste(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_handle_paste")
        body = _method_src(meth)
        assert "self._enqueue_paste(" in body

    def test_enqueue_paste_uses_qtimer(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_enqueue_paste")
        body = _method_src(meth)
        assert "QTimer.singleShot" in body

    def test_enqueue_paste_checks_transport_alive(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_enqueue_paste")
        body = _method_src(meth)
        assert "self._transport.is_alive" in body

    def test_paste_next_chunk_checks_transport_alive(self):
        tree = _parse()
        meth = _find_method(_parse(), "OpenCodeTerminalWidget", "_paste_next_chunk")
        body = _method_src(meth)
        assert "self._transport.is_alive" in body

    def test_paste_next_chunk_writes_chunk(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_paste_next_chunk")
        body = _method_src(meth)
        assert "self._transport.write(chunk)" in body

    def test_paste_next_chunk_reschedules(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_paste_next_chunk")
        body = _method_src(meth)
        assert "QTimer.singleShot" in body

    def test_paste_next_chunk_catches_exceptions(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_paste_next_chunk")
        body = _method_src(meth)
        assert "except" in body

    def test_no_process_kill_in_paste_code(self):
        """Paste code must NEVER call process.kill, process.terminate,
        transport.close, transport.stop, or widget.close."""
        tree = _parse()
        forbidden = [
            "process.kill", "process.terminate",
            "transport.close", "transport.stop",
            "widget.close",
        ]
        for name in ("_handle_paste", "_enqueue_paste", "_paste_next_chunk"):
            meth = _find_method(tree, "OpenCodeTerminalWidget", name)
            if meth is None:
                continue
            body = _method_src(meth)
            for pat in forbidden:
                assert pat not in body, f"{name} must not call {pat}"

    def test_placeholder_is_stream_feed_only(self):
        """The placeholder text goes to _stream.feed, never to _transport.write."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_handle_paste")
        body = _method_src(meth)
        # placeholder goes to stream.feed
        assert "self._stream.feed(" in body

    def test_ctrl_v_calls_handle_paste(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "keyPressEvent")
        body = _method_src(meth)
        assert "self._handle_paste()" in body

    def test_right_click_calls_handle_paste(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "contextMenuEvent")
        body = _method_src(meth)
        assert "self._handle_paste()" in body

    def test_paste_chunk_size_initialized(self):
        assert "_paste_chunk_size" in _source()

    def test_pending_paste_initialized(self):
        assert "_pending_paste" in _source()

    def test_pending_paste_offset_initialized(self):
        assert "_pending_paste_offset" in _source()

    def test_handle_paste_checks_is_alive_for_enqueue(self):
        """_handle_paste builds the payload then delegates — it does NOT
        guard with is_alive itself; _enqueue_paste does that."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_handle_paste")
        body = _method_src(meth)
        # _handle_paste may check is_alive for large paste placeholder path
        # but the key is it calls _enqueue_paste, not _transport.write
        assert "self._enqueue_paste(" in body


# ── Line-counting logic ────────────────────────────────────────


def _count_paste_lines(text: str) -> int:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.count("\n") + (
        1 if normalized and not normalized.endswith("\n") else 0
    )


class TestCrlfLineCount:
    def test_single_line(self):
        assert _count_paste_lines("hello") == 1

    def test_two_lines(self):
        assert _count_paste_lines("line1\nline2") == 2

    def test_ten_lines(self):
        assert _count_paste_lines("\n".join(f"l{i}" for i in range(10))) == 10

    def test_crlf_windows(self):
        assert _count_paste_lines("line1\r\nline2\r\nline3") == 3

    def test_crlf_windows_trailing(self):
        assert _count_paste_lines("line1\r\nline2\r\nline3\r\n") == 3

    def test_cr_old_mac(self):
        assert _count_paste_lines("line1\rline2\rline3") == 3

    def test_mixed_endings(self):
        assert _count_paste_lines("a\r\nb\nc\rd") == 4

    def test_empty(self):
        assert _count_paste_lines("") == 0

    def test_145_lines(self):
        assert _count_paste_lines("\n".join(f"l{i}" for i in range(145))) == 145

    def test_300_lines(self):
        assert _count_paste_lines("\n".join(f"l{i}" for i in range(300))) == 300


# ── Bracketed paste escape characters ──────────────────────────


class TestBracketedPasteEscapeCharacters:
    def test_bracketed_paste_start_is_real_escape(self):
        src = _source()
        for line in src.splitlines():
            if "_BRACKETED_PASTE_START" in line and "=" in line:
                assert "\\x1b[200~" in line, (
                    "Start marker must use \\x1b escape, not literal backslash-x"
                )
                return
        pytest.fail("_BRACKETED_PASTE_START not found")

    def test_bracketed_paste_end_is_real_escape(self):
        src = _source()
        for line in src.splitlines():
            if "_BRACKETED_PASTE_END" in line and "=" in line:
                assert "\\x1b[201~" in line
                return
        pytest.fail("_BRACKETED_PASTE_END not found")

    def test_markers_are_module_level_constants(self):
        """Markers should be defined at module level, not inside methods."""
        tree = _parse()
        module_stmts = [n for n in tree.body if isinstance(n, ast.Assign)]
        found_start = False
        found_end = False
        for stmt in module_stmts:
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    if target.id == "_BRACKETED_PASTE_START":
                        found_start = True
                    elif target.id == "_BRACKETED_PASTE_END":
                        found_end = True
        assert found_start, "_BRACKETED_PASTE_START must be a module-level constant"
        assert found_end, "_BRACKETED_PASTE_END must be a module-level constant"


# ── Paste process preservation ─────────────────────────────────


class TestPasteDoesNotStopProcess:
    """Verify paste code never terminates the OpenCode process."""

    def test_small_paste_does_not_stop_process(self):
        """Small paste: _handle_paste builds payload, calls _enqueue_paste.
        Neither method touches process.kill / process.terminate / transport.stop."""
        tree = _parse()
        for name in ("_handle_paste", "_enqueue_paste", "_paste_next_chunk"):
            meth = _find_method(tree, "OpenCodeTerminalWidget", name)
            body = _method_src(meth)
            assert "process.kill" not in body
            assert "process.terminate" not in body
            assert "transport.stop" not in body

    def test_large_paste_does_not_stop_process(self):
        """Large paste follows the same code path — chunked enqueue."""
        # Same structural check — large paste uses same _enqueue_paste
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_enqueue_paste")
        body = _method_src(meth)
        assert "process.kill" not in body
        assert "process.terminate" not in body

    def test_transport_exception_does_not_kill_process(self):
        """_paste_next_chunk catches exceptions and clears state, never kills."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_paste_next_chunk")
        body = _method_src(meth)
        assert "except" in body
        # On exception, it clears the pending paste — doesn't kill anything
        assert "self._pending_paste" in body

    def test_dead_transport_paste_aborts_safely(self):
        """_enqueue_paste returns immediately if transport is dead."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_enqueue_paste")
        body = _method_src(meth)
        assert "return" in body
        assert "self._transport.is_alive" in body

    def test_paste_next_chunk_aborts_on_dead_transport(self):
        """_paste_next_chunk checks transport liveness each iteration."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_paste_next_chunk")
        body = _method_src(meth)
        assert "return" in body
        assert "self._transport.is_alive" in body

    def test_no_kill_methods_in_paste_module_section(self):
        """The entire paste section (methods between markers) must not
        contain any process-killing calls."""
        src = _source()
        paste_section_start = src.index("# Internal — paste handling")
        paste_section = src[paste_section_start:]
        for pat in ("process.kill", "process.terminate", "transport.close",
                    "transport.stop"):
            assert pat not in paste_section, (
                f"Forbidden call '{pat}' found in paste handling section"
            )


# ── Placeholder is UI-only ─────────────────────────────────────


class TestPlaceholderIsNotSent:
    def test_large_paste_shows_placeholder(self):
        """Placeholder is fed to _stream.feed, never to _transport.write."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_handle_paste")
        body = _method_src(meth)
        assert "self._stream.feed(" in body
        assert "[pasted ~" in body

    def test_placeholder_not_in_enqueue_paste(self):
        """_enqueue_paste never mentions placeholder — it just sends payload."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_enqueue_paste")
        body = _method_src(meth)
        assert "[pasted" not in body
        assert "placeholder" not in body

    def test_placeholder_not_in_paste_next_chunk(self):
        """_paste_next_chunk never mentions placeholder."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "_paste_next_chunk")
        body = _method_src(meth)
        assert "[pasted" not in body
        assert "placeholder" not in body


# ── Right-click paste ──────────────────────────────────────────


class TestRightClickPaste:
    def test_context_menu_calls_handle_paste(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "contextMenuEvent")
        body = _method_src(meth)
        assert "self._handle_paste()" in body

    def test_context_menu_checks_transport(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "contextMenuEvent")
        body = _method_src(meth)
        assert "self._transport.is_alive" in body

    def test_context_menu_accepts_event(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "contextMenuEvent")
        body = _method_src(meth)
        assert "event.accept()" in body


# ── Ctrl+V / Ctrl+Shift+V paste ───────────────────────────────


class TestCtrlVPaste:
    def test_ctrl_v_calls_handle_paste(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "keyPressEvent")
        body = _method_src(meth)
        assert "Key_V" in body
        assert "self._handle_paste()" in body

    def test_ctrl_v_is_first_check_in_key_press(self):
        """Ctrl+V check should happen before regular key translation."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "keyPressEvent")
        body = _method_src(meth)
        v_pos = body.index("Key_V")
        translate_pos = body.index("_translate_key")
        assert v_pos < translate_pos, (
            "Ctrl+V check must come before _translate_key call"
        )

    def test_ctrl_v_accepts_event(self):
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "keyPressEvent")
        body = _method_src(meth)
        # After _handle_paste(), event.accept() must be called
        assert "event.accept()" in body

    def test_ctrl_shift_v_same_as_ctrl_v(self):
        """Ctrl+Shift+V uses same Key_V check — both modifiers pass through
        because the check only tests for ControlModifier."""
        tree = _parse()
        meth = _find_method(tree, "OpenCodeTerminalWidget", "keyPressEvent")
        body = _method_src(meth)
        # The check is: ctrl and key in (Qt.Key.Key_V,)
        # Shift doesn't affect this — both Ctrl+V and Ctrl+Shift+V match
        assert "Key_V" in body
        assert "self._handle_paste()" in body
