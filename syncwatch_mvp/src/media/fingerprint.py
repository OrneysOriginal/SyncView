from __future__ import annotations

import hashlib
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

BLOCK_SIZE = 1024 * 1024


def calculate_fingerprint(path: str | Path, *, cancel_event: Event | None = None,
                          block_hashes: list[bytes] | None = None) -> str:
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            block = fh.read(BLOCK_SIZE)
            if not block:
                break
            digest.update(block)
            if block_hashes is not None:
                block_hashes.append(hashlib.sha256(block).digest())
    return digest.hexdigest()
