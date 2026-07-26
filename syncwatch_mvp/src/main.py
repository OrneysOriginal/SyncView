from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from src.infrastructure.bundled_vlc import configure_bundled_vlc
from src.infrastructure.logger import configure_logging


def main() -> int:
    configure_logging()
    configure_bundled_vlc()

    # Import after configuring VLC search paths. This is required for packaged
    # builds because importing the UI imports python-vlc.
    from src.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("SyncWatch")
    try:
        window = MainWindow()
    except Exception as exc:  # pragma: no cover - environment dependent
        logging.exception("Application startup failed")
        QMessageBox.critical(
            None,
            "SyncWatch",
            "Не удалось запустить приложение. В автономной сборке libVLC "
            f"должен находиться внутри пакета.\n\nОшибка: {exc}",
        )
        return 1
    window.resize(1100, 720)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
