from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QProgressBar, QVBoxLayout, QWidget


class StartupScreen(QWidget):
    close_requested = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Запуск SyncWatch")
        self.setObjectName("startupScreen")
        self.setFixedSize(460, 300)
        self.setStyleSheet("""
            QWidget#startupScreen { background:#F7F8FA; border:1px solid #E3E6EC; }
            QLabel { color:#171A21; border:none; font-family:"Segoe UI","Arial"; }
            QLabel#startupLogo { background:#5B5FEF; color:white; border-radius:24px; font-size:24px; }
            QLabel#startupTitle { font-size:28px; font-weight:700; }
            QLabel#startupSubtitle, QLabel#startupHint { color:#707785; font-size:12px; }
            QLabel#startupStatus { font-size:13px; }
            QProgressBar { border:none; border-radius:3px; background:#E3E6EC; }
            QProgressBar::chunk { background:#5B5FEF; border-radius:3px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 28, 40, 26)
        layout.setSpacing(10)
        logo = QLabel("▶")
        logo.setObjectName("startupLogo")
        logo.setFixedSize(48, 48)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("SyncWatch")
        title.setObjectName("startupTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("Совместный просмотр видео")
        subtitle.setObjectName("startupSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status = QLabel("Запускаем приложение…")
        self.status.setObjectName("startupStatus")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        hint = QLabel("Подготовка может занять немного времени")
        hint.setObjectName("startupHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch()
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addWidget(hint)
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())

    @Slot(str)
    def set_stage(self, text: str) -> None:
        self.status.setText(text)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.close_requested.emit()
        super().closeEvent(event)


def prepare_application(progress: Callable[[str], None]) -> tuple[Any, Any]:
    # Heavy imports and native initialization must not block the Qt GUI loop.
    progress("Подготавливаем компоненты…")
    from src.infrastructure.bundled_vlc import configure_bundled_vlc

    configure_bundled_vlc()
    from src.ui.main_window import MainWindow
    from src.player.vlc_player import create_vlc_instance

    progress("Запускаем видеоплеер…")
    instance = create_vlc_instance()
    return MainWindow, instance


class StartupWorker(QThread):
    progress = Signal(str)
    ready = Signal(object, object)
    failed = Signal(str)

    def __init__(self, prepare: Callable, parent: QObject) -> None:
        super().__init__(parent)
        self.prepare = prepare

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        try:
            factory, instance = self.prepare(self.progress.emit)
            self.ready.emit(factory, instance)
        except Exception as exc:
            logging.exception("Application preparation failed")
            self.failed.emit(str(exc))


class StartupController(QObject):
    def __init__(
        self,
        app: QApplication,
        log_path: Path,
        *,
        smoke_test: bool = False,
        prepare: Callable = prepare_application,
    ) -> None:
        super().__init__(app)
        self.app = app
        self.log_path = log_path
        self.smoke_test = smoke_test
        self.cancelled = False
        self.window: QWidget | None = None
        self.screen = StartupScreen()
        self.worker = StartupWorker(prepare, self)
        self.worker.progress.connect(self.screen.set_stage)
        self.worker.ready.connect(self._ready)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        self.screen.close_requested.connect(self._cancel)

    def start(self) -> None:
        # Keep the event loop alive if the splash is closed while native VLC is
        # still initializing. The worker is allowed to finish before exiting.
        self.app.setQuitOnLastWindowClosed(False)
        self.screen.show()
        self.worker.start()

    @Slot(object, object)
    def _ready(self, factory: Callable, instance: Any) -> None:
        if self.cancelled:
            instance.release()
            return
        self.screen.set_stage("Открываем приложение…")
        try:
            self.window = factory(vlc_instance=instance)
            self.window.resize(1100, 720)
            self.window.show()
        except Exception as exc:
            instance.release()
            logging.exception("Application window creation failed")
            self._failed(str(exc))
            return
        self.screen.hide()
        self.app.setQuitOnLastWindowClosed(True)
        if self.smoke_test:
            QTimer.singleShot(1000, self.window.close)

    @Slot(str)
    def _failed(self, reason: str) -> None:
        self.screen.hide()
        if self.cancelled:
            return
        if not self.smoke_test:
            QMessageBox.critical(
                None,
                "SyncWatch",
                f"Не удалось запустить приложение.\n\n{reason}\n\nПодробности: {self.log_path}",
            )
        self.app.exit(1)

    @Slot()
    def _cancel(self) -> None:
        self.cancelled = True
        self.worker.requestInterruption()
        if not self.worker.isRunning():
            self.app.quit()

    @Slot()
    def _finished(self) -> None:
        if self.cancelled:
            self.app.quit()
