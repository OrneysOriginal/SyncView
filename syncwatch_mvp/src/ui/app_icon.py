from __future__ import annotations

import ctypes
import logging
import sys

from PySide6.QtGui import QIcon

from src.infrastructure.bundled_vlc import application_root


def application_icon() -> QIcon:
    return QIcon(str(application_root() / "assets" / "syncwatch.ico"))


def configure_windows_identity() -> None:
    """Group Windows taskbar windows under SyncWatch's own application identity."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SyncWatch.Desktop")
        except (AttributeError, OSError):
            logging.exception("Could not set Windows taskbar application identity")
