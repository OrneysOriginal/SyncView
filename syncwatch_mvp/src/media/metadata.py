from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class MediaInfo:
    file_name: str
    file_size: int
    duration_ms: int
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "MediaInfo":
        return cls(
            file_name=str(data["file_name"]),
            file_size=int(data["file_size"]),
            duration_ms=int(data["duration_ms"]),
            fingerprint=str(data["fingerprint"]),
        )


def basic_file_data(path: str | Path) -> tuple[str, int]:
    p = Path(path)
    return p.name, p.stat().st_size
