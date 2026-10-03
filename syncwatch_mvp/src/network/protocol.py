from __future__ import annotations

import json
import math
from typing import Any

from src.network.messages import PROTOCOL_VERSION, Message

MAX_MESSAGE_SIZE = 1 * 1024 * 1024
ALLOWED_TYPES = {
    "join", "join_accepted", "join_rejected", "disconnect", "error", "ping", "pong",
    "media_info", "media_match", "media_mismatch", "media_force_match",
    "ready", "not_ready",
    "play", "pause", "seek", "stop", "state", "sync_correction",
    "seek_ack", "seek_complete", "seek_failed",
    "session_started", "session_ended", "client_reconnected",
    "file_offer", "file_accept", "file_reject", "file_cancel",
    "file_chunk", "file_complete", "file_error",
    "file_received",
}

HOST_COMMANDS = {"play", "pause", "seek", "stop", "sync_correction", "media_force_match", "session_ended",
                 "file_offer", "seek_complete"}


def validate_payload(kind: str, data: dict) -> None:
    def integer(key: str, minimum: int = 0, maximum: int = 10**12) -> None:
        value = data.get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Некорректное поле {key}")

    def string(key: str, maximum: int = 1024) -> None:
        value = data.get(key)
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError(f"Некорректное поле {key}")

    def boolean(key: str) -> None:
        if type(data.get(key)) is not bool:
            raise ValueError(f"Некорректное поле {key}")

    if kind == "join":
        string("room_code", 32)
        if "device_name" in data:
            string("device_name", 128)
    if kind in {"media_info", "file_offer"}:
        string("file_name", 255)
        string("fingerprint", 128)
        integer("file_size", 1, 16 * 1024**4)
        if "duration_ms" in data:
            integer("duration_ms")
        elif kind == "media_info":
            raise ValueError("Отсутствует duration_ms")
    if kind in {"ready", "not_ready"}:
        boolean("media_loaded")
    if kind in {"play", "pause", "seek", "sync_correction"}:
        integer("command_id", 1)
        integer("position_ms")
        integer("execute_delay_ms", 0, 5000)
        string("media_fingerprint", 128)
        if "execute_at" not in data:
            raise ValueError("Отсутствует время команды")
        if "execute_at" in data and (type(data["execute_at"]) not in (float, int)
                                     or not math.isfinite(data["execute_at"])):
            raise ValueError("Некорректное время команды")
        if "resume" in data:
            boolean("resume")
        if "seek_feedback_ms" in data:
            integer("seek_feedback_ms", -10**12, 10**12)
    if kind == "state":
        integer("position_ms")
        boolean("playing")
        integer("last_command_id")
    if kind in {"seek_ack", "seek_complete", "seek_failed"}:
        integer("seek_id", 1)
        string("media_fingerprint", 128)
    if kind == "seek_ack":
        integer("position_ms")
        boolean("paused")
    if kind == "seek_failed":
        string("reason")
    if kind.startswith("file_"):
        string("transfer_id", 128)
    if kind == "file_chunk":
        integer("index")
        string("data", 700000)
    if kind == "file_complete":
        string("fingerprint", 128)
    if kind == "pong":
        for key in ("echo", "remote_time"):
            if type(data.get(key)) not in (float, int) or not math.isfinite(data[key]):
                raise ValueError("Некорректные метрики часов")


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
    if type(data.get("protocol_version")) is not int:
        raise ValueError("Некорректная версия протокола")
    if not isinstance(data.get("type"), str):
        raise ValueError("Некорректный тип сообщения")
    if not isinstance(data.get("message_id"), str) or not 0 < len(data["message_id"]) <= 128:
        raise ValueError("Некорректный ID сообщения")
    if not isinstance(data.get("session_id", ""), str) or len(data.get("session_id", "")) > 128:
        raise ValueError("Некорректный ID сессии")
    if type(data.get("sent_at")) not in (float, int) or not math.isfinite(data["sent_at"]):
        raise ValueError("Некорректное время сообщения")
    message = Message.from_dict(data)
    if message.type not in ALLOWED_TYPES:
        raise ValueError(f"Неизвестный тип сообщения: {message.type}")
    if message.protocol_version != PROTOCOL_VERSION:
        raise ValueError("Несовместимая версия протокола")
    validate_payload(message.type, message.payload)
    return message
