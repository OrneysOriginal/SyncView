"""Regenerate the Windows ICO from the source SVG using the project's Qt dependency."""
from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> None:
    assets = Path(__file__).resolve().parents[1] / "assets"
    renderer = QSvgRenderer(str(assets / "syncwatch.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid application icon SVG")
    frames = []
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise RuntimeError("Cannot encode application icon")
        frames.append((size, bytes(encoded)))
    offset = 6 + 16 * len(frames)
    entries = []
    for size, data in frames:
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                                   len(data), offset))
        offset += len(data)
    (assets / "syncwatch.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries)
        + b"".join(data for _, data in frames)
    )


if __name__ == "__main__":
    main()
