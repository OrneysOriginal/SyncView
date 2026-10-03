from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 3


@dataclass(slots=True)
class Message:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION
    session_id: str = ""
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sent_at: float = field(default_factory=time.monotonic)
    # Local transport metadata, never taken from the wire.
    sender_id: str = ""
    received_at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.payload,
            "type": self.type,
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        required = {"type", "protocol_version", "message_id", "sent_at"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Отсутствуют поля: {', '.join(sorted(missing))}")
        payload = {k: v for k, v in data.items() if k not in required | {"session_id"}}
        return cls(
            type=str(data["type"]),
            protocol_version=int(data["protocol_version"]),
            session_id=str(data.get("session_id", "")),
            message_id=str(data["message_id"]),
            sent_at=float(data["sent_at"]),
            payload=payload,
        )
