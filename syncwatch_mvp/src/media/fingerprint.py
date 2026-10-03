from __future__ import annotations

import hashlib
from pathlib import Path

BLOCK_SIZE = 1024 * 1024


def calculate_fingerprint(path: str | Path) -> str:
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        for block in iter(lambda: fh.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()
