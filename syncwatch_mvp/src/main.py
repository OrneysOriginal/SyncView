from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from src.infrastructure.logger import configure_logging
from src.ui.startup import StartupController


def main() -> int:
    log_path = configure_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("SyncWatch")
    startup = StartupController(app, log_path, smoke_test="--smoke-test" in sys.argv)
    startup.start()
    try:
        return app.exec()
    finally:
        startup.worker.wait()


if __name__ == "__main__":
    raise SystemExit(main())
