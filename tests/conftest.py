"""Shared Qt fixtures for the root test suite."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def window(qapp, monkeypatch):
    """MainWindow with background threads disabled."""
    import gc

    from app.ui.main_window import MainWindow
    from app.agents.pipeline import PlanWorker
    from app.agents.chat_workers import ChatWorker, OpenCodeChatWorker

    monkeypatch.setattr(MainWindow, "_start_backend_detection", lambda self: None)
    monkeypatch.setattr(PlanWorker, "start", lambda self: None)
    monkeypatch.setattr(ChatWorker, "start", lambda self: None)
    monkeypatch.setattr(OpenCodeChatWorker, "start", lambda self: None)
    win = MainWindow()
    yield win
    try:
        win.close()
    except Exception:
        pass
    # Destroy C++ objects deterministically instead of leaving them to a
    # garbage-collection cascade between tests (prevents access violations).
    win.deleteLater()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del win
    gc.collect()
