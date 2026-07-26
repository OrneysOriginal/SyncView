from __future__ import annotations

import logging
from pathlib import Path


def configure_logging() -> Path:
    log_dir = Path.home() / ".syncwatch" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "syncwatch.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )
    return log_file
