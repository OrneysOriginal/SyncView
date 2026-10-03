from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging() -> Path:
    log_dir = Path.home() / ".syncwatch" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "syncwatch.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )
    return log_file
