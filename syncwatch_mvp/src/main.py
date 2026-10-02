from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer

from src.infrastructure.bundled_vlc import configure_bundled_vlc
from src.infrastructure.logger import configure_logging


def main() -> int:
    configure_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("SyncWatch")
    try:
        configure_bundled_vlc()
        # Import after configuring VLC paths, inside the startup error handler.
        from src.ui.main_window import MainWindow

        window = MainWindow()
    except Exception as exc:  # pragma: no cover - environment dependent
        logging.exception("Application startup failed")
        if "--smoke-test" in sys.argv:
            return 1
        QMessageBox.critical(
            None,
            "SyncWatch",
            "Не удалось запустить приложение. В автономной сборке libVLC "
            f"должен находиться внутри пакета.\n\nОшибка: {exc}",
        )
        return 1
    window.resize(1100, 720)
    window.show()
    if "--smoke-test" in sys.argv:
        # Packaging check: create Qt, VLC and the network thread, then close
        # through the normal window shutdown path. No video is played.
        QTimer.singleShot(1000, window.close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
