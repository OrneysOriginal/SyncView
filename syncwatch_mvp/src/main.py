from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from src.infrastructure.logger import configure_logging
from src.ui.app_icon import application_icon, configure_windows_identity
from src.ui.startup import StartupController


def main() -> int:
    log_path = configure_logging()
    configure_windows_identity()
    app = QApplication(sys.argv)
    app.setApplicationName("SyncWatch")
    app.setWindowIcon(application_icon())
    startup = StartupController(app, log_path, smoke_test="--smoke-test" in sys.argv)
    startup.start()
    try:
        return app.exec()
    finally:
        if startup.window is not None:
            startup.window.close()
        startup.worker.wait()


if __name__ == "__main__":
    raise SystemExit(main())
