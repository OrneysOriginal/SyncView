from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Condition, Event

from src.media.fingerprint import BLOCK_SIZE
from src.media.metadata import MediaInfo

HASH_BATCH_SIZE = 64
CHECK_REPLY_TIMEOUT = 10.0


class LocalFileCheck:
    """Compare sequential blocks against bounded batches from the host's index."""

    def __init__(self, path: Path, expected: MediaInfo, cancel_event: Event,
                 request: Callable[[dict], None], progress: Callable[[int], None]) -> None:
        self.path, self.expected, self.cancel_event = path, expected, cancel_event
        self.request, self.progress = request, progress
        self.check_id = uuid.uuid4().hex
        self._condition = Condition()
        self._offset = -1
        self._reply: dict | None = None

    def accept_reply(self, payload: dict) -> None:
        with self._condition:
            if (payload.get("check_id") != self.check_id
                    or payload.get("media_fingerprint") != self.expected.fingerprint
                    or payload.get("offset") != self._offset or self._reply is not None):
                return
            self._reply = payload
            self._condition.notify_all()

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError()

    def _hashes(self, offset: int, count: int) -> list[str]:
        self._check_cancelled()
        with self._condition:
            self._offset, self._reply = offset, None
        self.request({"check_id": self.check_id, "media_fingerprint": self.expected.fingerprint,
                      "offset": offset, "count": count})
        deadline = time.monotonic() + CHECK_REPLY_TIMEOUT
        with self._condition:
            while self._reply is None:
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Ведущий не ответил на проверку файла. Повторите выбор видео.")
                self._condition.wait(min(remaining, 0.05))
            self._check_cancelled()
            reply = self._reply
        if reply.get("reason"):
            raise ValueError(reply["reason"])
        hashes = reply.get("hashes")
        if not isinstance(hashes, list) or len(hashes) != count:
            raise ValueError("Ведущий прислал неполные данные проверки")
        return hashes

    def run(self) -> MediaInfo:
        self._check_cancelled()
        before = self.path.stat()
        if before.st_size != self.expected.file_size:
            raise ValueError("Файлы разные: размер выбранного файла отличается от файла ведущего")
        digest = hashlib.sha256()
        count = (before.st_size + BLOCK_SIZE - 1) // BLOCK_SIZE
        self.progress(0)
        last_percent = 0
        with self.path.open("rb") as source:
            for offset in range(0, count, HASH_BATCH_SIZE):
                hashes = self._hashes(offset, min(HASH_BATCH_SIZE, count - offset))
                for index, expected_hash in enumerate(hashes, offset):
                    self._check_cancelled()
                    block = source.read(min(BLOCK_SIZE, before.st_size - index * BLOCK_SIZE))
                    if hashlib.sha256(block).hexdigest() != expected_hash:
                        raise ValueError(f"Файлы разные: найдено отличие в блоке {index + 1}. Проверка остановлена.")
                    digest.update(block)
                    percent = (index + 1) * 100 // count
                    if percent != last_percent:
                        self.progress(percent)
                        last_percent = percent
        self._check_cancelled()
        after = self.path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise ValueError("Выбранный файл изменился во время проверки")
        if digest.hexdigest() != self.expected.fingerprint:
            raise ValueError("Файлы разные: полная контрольная сумма не совпадает")
        return MediaInfo(self.path.name, before.st_size, 0, digest.hexdigest())
