from __future__ import annotations

import hashlib
from pathlib import Path

BLOCK_SIZE = 1024 * 1024


def calculate_fingerprint(path: str | Path) -> str:
    file_path = Path(path)
    size = file_path.stat().st_size
    positions = [0, max(0, size // 2 - BLOCK_SIZE // 2), max(0, size - BLOCK_SIZE)]
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        for position in positions:
            fh.seek(position)
            digest.update(fh.read(BLOCK_SIZE))
    return digest.hexdigest()
