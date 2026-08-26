"""QProcess / worker lifecycle cleanup tests.

Regression coverage for:
    RuntimeError: libshiboken: Internal C++ object (QProcess) already deleted.
"""

import sys
import os
import gc

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PySide6.QtCore import QEvent

from app.ui.main_window import MainWindow


class FakeCloseEvent:
    def __init__(self):
        self.accepted = False

    def accept(self):
        self.accepted = True


def make_window(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_start_backend_detection", lambda self: None)
    return MainWindow()


def destroy_window(qapp, window):
    """Deterministic C++ teardown to avoid shutdown access violations."""
    window.deleteLater()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


def test_close_event_accepts_cleanly(qapp, monkeypatch):
    window = make_window(qapp, monkeypatch)
    event = FakeCloseEvent()
    window.closeEvent(event)
    assert event.accepted is True
    destroy_window(qapp, window)


def test_close_event_is_idempotent(qapp, monkeypatch):
    window = make_window(qapp, monkeypatch)
    window.closeEvent(FakeCloseEvent())
    # A second pass (e.g. re-close after workers cleared) must not raise.
    window.closeEvent(FakeCloseEvent())
    destroy_window(qapp, window)


def test_close_with_running_pipeline_thread(qapp, monkeypatch):
    from app.agents.pipeline import ApprovalPipeline

    window = make_window(qapp, monkeypatch)
    pipeline = ApprovalPipeline("task", str(window.project_root()), parent=window)
    window._pipeline = pipeline
    # Do not start the thread — closeEvent must still handle it safely.
    window.closeEvent(FakeCloseEvent())
    destroy_window(qapp, window)


def test_main_window_has_no_long_lived_qprocess_members(qapp, monkeypatch):
    """The old embedded-terminal QProcess map caused shiboken crashes on
    teardown; the IDE must not keep QProcess objects as instance state."""
    from PySide6.QtCore import QProcess

    window = make_window(qapp, monkeypatch)
    for name, value in vars(window).items():
        assert not isinstance(value, QProcess), f"{name} holds a raw QProcess"
        if isinstance(value, dict):
            for inner in value.values():
                assert not isinstance(inner, QProcess)
    destroy_window(qapp, window)


def test_opencode_process_cleanup_safe_without_start():
    from app.agents.opencode.process import OpenCodeProcess

    proc = OpenCodeProcess()
    proc.cleanup()
    proc.cleanup()  # double cleanup must be safe
