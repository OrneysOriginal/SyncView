from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from src.media.metadata import MediaInfo

CHUNK_SIZE = 512 * 1024
SendFn = Callable[[str, dict], None]
ProgressFn = Callable[[float, str], None]


def received_dir() -> Path:
    path = Path.home() / ".syncwatch" / "received"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    cleaned = re.sub(r"[^\w.\- ()\[\]]+", "_", base, flags=re.UNICODE).strip(" ._")
    if cleaned.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        cleaned = "_" + cleaned
    cleaned = cleaned[:180]
    while len(cleaned.encode("utf-8")) > 180:
        cleaned = cleaned[:-1]
    return cleaned or "video.bin"


class FileTransferSender:
    def __init__(
        self,
        path: str | Path,
        media: MediaInfo,
        send: SendFn,
        on_progress: ProgressFn | None = None,
        transfer_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.media = media
        self.send = send
        self.on_progress = on_progress or (lambda _fraction, _text: None)
        self.transfer_id = transfer_id or uuid.uuid4().hex
        self.cancel_event = threading.Event()

    def offer_payload(self) -> dict[str, object]:
        return {
            "transfer_id": self.transfer_id,
            "file_name": self.media.file_name,
            "file_size": self.media.file_size,
            "fingerprint": self.media.fingerprint,
            "duration_ms": self.media.duration_ms,
        }

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        size = self.path.stat().st_size
        if size != self.media.file_size:
            self.send(
                "file_error",
                {
                    "transfer_id": self.transfer_id,
                    "reason": "Размер файла изменился после выбора",
                },
            )
            raise ValueError("Размер файла изменился после выбора")

        total = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
        sent = 0
        digest = hashlib.sha256()
        with self.path.open("rb") as fh:
            index = 0
            while True:
                if self.cancel_event.is_set():
                    self.send("file_cancel", {"transfer_id": self.transfer_id})
                    return
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                self.send(
                    "file_chunk",
                    {
                        "transfer_id": self.transfer_id,
                        "index": index,
                        "total": total,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                sent += len(chunk)
                index += 1
                fraction = min(1.0, sent / size) if size else 1.0
                self.on_progress(fraction, f"Отправка {int(fraction * 100)}%…")

        if self.cancel_event.is_set():
            self.send("file_cancel", {"transfer_id": self.transfer_id})
            return

        if digest.hexdigest() != self.media.fingerprint:
            self.send("file_error", {"transfer_id": self.transfer_id, "reason": "Файл изменился после выбора"})
            raise ValueError("Файл изменился после выбора")
        self.send(
            "file_complete",
            {
                "transfer_id": self.transfer_id,
                "fingerprint": digest.hexdigest(),
            },
        )
        self.on_progress(1.0, "Файл отправлен. Ожидание открытия на другом устройстве…")


class FileTransferReceiver:
    def __init__(self, offer: dict[str, object], directory: Path | None = None) -> None:
        self.transfer_id = str(offer["transfer_id"])
        self.file_name = safe_filename(str(offer["file_name"]))
        self.file_size = int(offer["file_size"])
        if self.file_size <= 0:
            raise ValueError("Размер файла должен быть положительным")
        self.expected_fingerprint = str(offer["fingerprint"])
        self.duration_ms = int(offer.get("duration_ms", 0))
        self.directory = directory or received_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.directory).free < self.file_size + 1024 * 1024:
            raise OSError("Недостаточно места для получения файла")
        self.final_path = self._unique_path(self.directory / self.file_name)
        self.part_path = self.directory / f".{uuid.uuid4().hex}.part"
        self.received = 0
        self.next_index = 0
        self._fh = self.part_path.open("xb")
        self._digest = hashlib.sha256()
        self.cancel_event = threading.Event()

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        for index in range(1, 10_000):
            candidate = parent / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return candidate
        raise RuntimeError("Не удалось подобрать имя файла для сохранения")

    def progress(self) -> float:
        if self.file_size <= 0:
            return 1.0
        return min(1.0, self.received / self.file_size)

    def write_chunk(self, index: int, data_b64: str) -> float:
        if self.cancel_event.is_set():
            raise RuntimeError("Передача отменена")
        if index != self.next_index:
            raise ValueError(f"Ожидался чанк {self.next_index}, получен {index}")
        raw = base64.b64decode(data_b64, validate=True)
        if not raw or len(raw) > CHUNK_SIZE:
            raise ValueError("Некорректный размер чанка")
        if self.received + len(raw) > self.file_size:
            raise ValueError("Получено больше данных, чем заявлено в размере файла")
        self._fh.write(raw)
        self._digest.update(raw)
        self.received += len(raw)
        if self.received > self.file_size:
            raise ValueError("Получено больше данных, чем заявлено в размере файла")
        self.next_index += 1
        return self.progress()

    def finalize(self, fingerprint: str) -> Path:
        if self.cancel_event.is_set():
            self.cleanup()
            raise RuntimeError("Передача отменена")
        self._fh.flush()
        self._fh.close()
        self._fh = None  # type: ignore[assignment]
        if self.received != self.file_size:
            self.cleanup()
            raise ValueError("Размер полученного файла не совпадает с заявленным")
        actual = self._digest.hexdigest()
        if actual != fingerprint or actual != self.expected_fingerprint:
            self.cleanup()
            raise ValueError("Отпечаток полученного файла не совпадает")
        if self.cancel_event.is_set():
            self.cleanup()
            raise RuntimeError("Передача отменена")
        # Atomic creation without overwriting a file created during transfer.
        self.final_path = self._unique_path(self.directory / self.file_name)
        try:
            os.link(self.part_path, self.final_path)
        except FileExistsError:
            self.cleanup()
            raise ValueError("Имя полученного файла уже занято")
        except OSError:
            # Filesystems without hard links: exclusive creation is still safe.
            created = False
            try:
                with self.final_path.open("xb") as output, self.part_path.open("rb") as source:
                    created = True
                    shutil.copyfileobj(source, output, CHUNK_SIZE)
            except Exception:
                if created:
                    self.final_path.unlink(missing_ok=True)
                self.cleanup()
                raise
        self.part_path.unlink()
        return self.final_path

    def cancel(self) -> None:
        self.cancel_event.set()
        self.cleanup()

    def cleanup(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None  # type: ignore[assignment]
        if self.part_path.exists():
            try:
                self.part_path.unlink()
            except OSError:
                pass
