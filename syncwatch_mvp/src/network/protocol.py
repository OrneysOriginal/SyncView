from __future__ import annotations

import json
from typing import Any

from src.network.messages import Message, PROTOCOL_VERSION

MAX_MESSAGE_SIZE = 1 * 1024 * 1024
ALLOWED_TYPES = {
    "join", "join_accepted", "join_rejected", "disconnect", "error", "ping", "pong",
    "media_info", "media_match", "media_mismatch", "media_force_match",
    "ready", "not_ready",
    "play", "pause", "seek", "stop", "state", "sync_correction",
    "session_started", "session_ended", "client_reconnected",
    "file_offer", "file_accept", "file_reject", "file_cancel",
    "file_chunk", "file_complete", "file_error",
}


def encode_message(message: Message) -> str:
    return json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))


def decode_message(raw: str | bytes) -> Message:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_MESSAGE_SIZE:
        raise ValueError("Сообщение слишком велико")
    data: Any = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Ожидался JSON-объект")
    message = Message.from_dict(data)
    if message.type not in ALLOWED_TYPES:
        raise ValueError(f"Неизвестный тип сообщения: {message.type}")
    if message.protocol_version != PROTOCOL_VERSION:
        raise ValueError("Несовместимая версия протокола")
    return message
