from __future__ import annotations

import importlib
import os
import sys
import time
from types import ModuleType
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def qt_app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window_factory(qt_app, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QMessageBox
    vlc = ModuleType("vlc")
    monkeypatch.setitem(sys.modules, "vlc", vlc)
    module = importlib.import_module("src.ui.main_window")
    real_network = module.NetworkThread

    class Player:
        def __init__(self, widget, **kwargs):
            self.position = 0
            self.playing = False
            self.duration = 60000
            self.started = 0.0
            self.ready = True
            self.error = False
            self.opened = []
            self.released = False
        def open(self, path):
            self.opened.append(path)
            self.stop()
        def get_position_ms(self):
            return self.position + (round((time.monotonic()-self.started)*1000) if self.playing else 0)
        def get_duration_ms(self):
            return self.duration
        def is_playing(self):
            return self.playing
        def play(self):
            if not self.playing:
                self.started = time.monotonic()
                self.playing = True
        def pause(self):
            self.position = self.get_position_ms()
            self.playing = False
        def stop(self):
            self.playing = False
            self.position = 0
        def seek(self, position):
            self.position = position
            self.started = time.monotonic()
        def has_error(self):
            return self.error
        def is_ready(self):
            return self.ready and self.duration > 0
        def set_volume(self, value):
            pass
        def bind_video_output(self):
            pass
        def release(self):
            self.released = True

    class Network(QObject):
        message_received = Signal(object)
        status_changed = Signal(str)
        failed = Signal(str)
        def __init__(self):
            super().__init__()
            for name in ("start", "send", "send_wait", "host", "connect_to", "leave_room",
                         "start_receiving", "stop_receiving", "shutdown"):
                setattr(self, name, Mock())

    monkeypatch.setattr(module, "VlcPlayer", Player)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    windows = []
    def create(real=False):
        monkeypatch.setattr(module, "NetworkThread", real_network if real else Network)
        window = module.MainWindow()
        window.test_errors = errors
        windows.append(window)
        if real:
            assert window.network._ready.wait(1)
        return window
    yield create
    for window in windows:
        if not window._closing:
            window.close()
    qt_app.processEvents()


def wait_until(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    app.processEvents()
    assert predicate(), "Timed out waiting for Qt event"
