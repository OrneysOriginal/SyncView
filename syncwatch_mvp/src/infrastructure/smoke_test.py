"""Packaged runtime check using a generated AVI, without external programs."""
from __future__ import annotations

import logging
import struct
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QTimer


def create_test_video(path: Path) -> None:
    width, height, frames, fps = 32, 24, 80, 10
    frame_size = width * height * 4

    def chunk(kind, data):
        return kind + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")

    avih = struct.pack("<14I", 100000, frame_size * fps, 0, 16, frames, 0, 1,
                       frame_size, width, height, 0, 0, 0, 0)
    strh = struct.pack("<4s4sIHHIIIIIIIIhhhh", b"vids", b"DIB ", 0, 0, 0, 0,
                       1, fps, 0, frames, frame_size, 0xFFFFFFFF, 0, 0, 0, width, height)
    strf = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, 0, frame_size, 0, 0, 0, 0)
    headers = chunk(b"LIST", b"hdrl" + chunk(b"avih", avih) +
                    chunk(b"LIST", b"strl" + chunk(b"strh", strh) + chunk(b"strf", strf)))
    movie = b"".join(chunk(b"00db", bytes((i * 3 % 256, 80, 120, 0)) * (width * height)) for i in range(frames))
    index = b"".join(struct.pack("<4sIII", b"00db", 16, 4 + i * (8 + frame_size), frame_size)
                     for i in range(frames))
    body = b"AVI " + headers + chunk(b"LIST", b"movi" + movie) + chunk(b"idx1", index)
    path.write_bytes(chunk(b"RIFF", body))


class PlaybackSmokeTest:
    def __init__(self, app, window) -> None:
        self.app, self.window = app, window
        self.directory = tempfile.TemporaryDirectory(prefix="syncwatch-smoke-")
        self.path = Path(self.directory.name) / "sample.avi"
        self.stage = "ready"
        self.deadline = time.monotonic() + 15
        self.timer = QTimer(window)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)

    def start(self) -> None:
        try:
            create_test_video(self.path)
            self.window.player.open(str(self.path))
            self.timer.start()
        except Exception:
            logging.exception("Packaged playback check failed")
            self.finish(1)

    def tick(self) -> None:
        player = self.window.player
        try:
            if player.has_error() or time.monotonic() > self.deadline:
                raise RuntimeError(f"VLC playback check failed at {self.stage}")
            if self.stage == "ready" and player.is_ready():
                player.pause()
                player.seek(2000)
                self.stage = "seek"
            elif self.stage == "seek" and abs(player.get_position_ms() - 2000) < 250:
                player.play()
                self.stage = "play"
            elif (self.stage == "play" and player.is_playing() and player.get_position_ms() > 2200
                  and player.video_frames_decoded() > 0):
                player.pause()
                self.stage = "pause"
            elif self.stage == "pause" and not player.is_playing():
                player.seek(5000)
                self.stage = "final_seek"
            elif self.stage == "final_seek" and abs(player.get_position_ms() - 5000) < 250:
                logging.info("VLC decode/play/pause/seek check passed")
                self.finish(0)
        except Exception:
            logging.exception("Packaged playback check failed")
            self.finish(1)

    def finish(self, code: int) -> None:
        self.timer.stop()
        self.window.close()
        self.directory.cleanup()
        self.app.exit(code)
