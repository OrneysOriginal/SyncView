"""Real Qt startup checks; no VLC or native video output is required."""
from __future__ import annotations

import os
import threading
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from src.ui.startup import StartupController


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    for window in application.topLevelWidgets():
        window.hide()


def execute(app, controller, unblock=None):
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: app.exit(99))
    watchdog.start(3000)
    controller.start()
    try:
        return app.exec()
    finally:
        watchdog.stop()
        if unblock is not None:
            unblock.set()
        controller.worker.wait()
        controller.screen.hide()
        if controller.window is not None:
            controller.window.hide()


def test_screen_remains_responsive_and_window_is_created_on_gui_thread(app, tmp_path):
    main_thread = threading.get_ident()
    release = threading.Event()
    observations = []
    ticks = []
    instance = Mock()

    def factory(*, vlc_instance):
        observations.append(("window", threading.get_ident(), vlc_instance))
        assert controller.screen.isVisible()
        window = QWidget()
        QTimer.singleShot(20, window.close)
        return window

    def prepare(progress):
        observations.append(("prepare", threading.get_ident(), None))
        progress("Запускаем видеоплеер…")
        assert release.wait(2), "The GUI did not process its timer during startup"
        return factory, instance

    controller = StartupController(app, tmp_path / "startup.log", prepare=prepare)
    heartbeat = QTimer()

    def tick():
        ticks.append(controller.screen.status.text())
        if len(ticks) >= 5:
            release.set()

    heartbeat.timeout.connect(tick)
    heartbeat.start(10)
    try:
        assert execute(app, controller, release) == 0
    finally:
        heartbeat.stop()
    assert observations[0][1] != main_thread
    assert observations[1] == ("window", main_thread, instance)
    assert "Запускаем видеоплеер…" in ticks
    assert not controller.screen.isVisible()
    assert controller.screen.progress.minimum() == controller.screen.progress.maximum() == 0


@pytest.mark.parametrize("smoke_test", [False, True])
def test_preparation_failure_hides_screen_and_reports_error(app, tmp_path, monkeypatch, smoke_test):
    def prepare(progress):
        raise RuntimeError("Missing VLC plugins")

    controller = StartupController(app, tmp_path / "startup.log", prepare=prepare, smoke_test=smoke_test)
    dialogs = []

    def dialog(*args):
        assert not controller.screen.isVisible()
        dialogs.append(args[-1])

    monkeypatch.setattr("src.ui.startup.QMessageBox.critical", dialog)
    assert execute(app, controller) == 1
    assert bool(dialogs) is not smoke_test
    if dialogs:
        assert "Missing VLC plugins" in dialogs[0]
        assert str(tmp_path / "startup.log") in dialogs[0]


def test_window_failure_releases_prepared_vlc(app, tmp_path):
    instance = Mock()

    def factory(**kwargs):
        raise RuntimeError("Window creation failed")

    controller = StartupController(
        app, tmp_path / "startup.log", prepare=lambda progress: (factory, instance), smoke_test=True,
    )
    assert execute(app, controller) == 1
    instance.release.assert_called_once()


def test_smoke_test_opens_and_automatically_closes_window(app, tmp_path):
    instance = Mock()
    controller = StartupController(
        app, tmp_path / "startup.log",
        prepare=lambda progress: (lambda **kwargs: QWidget(), instance),
        smoke_test=True,
    )
    assert execute(app, controller) == 0
    assert controller.window is not None
    assert not controller.window.isVisible()
    instance.release.assert_not_called()


def test_closing_screen_during_preparation_waits_and_does_not_open_window(app, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    instance = Mock()
    factory = Mock()

    def prepare(progress):
        entered.set()
        assert release.wait(2)
        return factory, instance

    controller = StartupController(app, tmp_path / "startup.log", prepare=prepare)
    timer = QTimer()

    def cancel():
        if entered.is_set():
            controller.screen.close()
            timer.stop()
            QTimer.singleShot(20, release.set)

    timer.timeout.connect(cancel)
    timer.start(10)
    try:
        assert execute(app, controller, release) == 0
    finally:
        timer.stop()
    factory.assert_not_called()
    instance.release.assert_called_once()
    assert not controller.worker.isRunning()
