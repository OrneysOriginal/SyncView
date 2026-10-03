from __future__ import annotations

import json

import pytest

from src.media.fingerprint import calculate_fingerprint
from src.media.metadata import MediaInfo
from src.network.file_transfer import FileTransferReceiver, FileTransferSender, safe_filename
from src.network.messages import PROTOCOL_VERSION, Message
from src.network.protocol import decode_message, encode_message


def offer(content, tmp_path):
    source = tmp_path / "original"
    source.write_bytes(content)
    return {"transfer_id": "t", "file_name": "clip", "file_size": len(content),
            "fingerprint": calculate_fingerprint(source)}


def test_overflow_is_rejected_before_writing(tmp_path):
    receiver = FileTransferReceiver(offer(b"a", tmp_path), tmp_path / "received")
    try:
        with pytest.raises(ValueError, match="больше"):
            receiver.write_chunk(0, "YWI=")
        assert receiver.received == 0 and receiver.part_path.stat().st_size == 0
    finally:
        receiver.cleanup()


def test_concurrent_receivers_do_not_share_part_file_or_overwrite_result(tmp_path):
    data = offer(b"abc", tmp_path)
    first = FileTransferReceiver(data, tmp_path / "received")
    second = FileTransferReceiver(data, tmp_path / "received")
    assert first.part_path != second.part_path
    for receiver in (first, second):
        receiver.write_chunk(0, "YWJj")
        receiver.finalize(data["fingerprint"])
    assert first.final_path != second.final_path
    assert first.final_path.read_bytes() == second.final_path.read_bytes() == b"abc"


def test_cancelled_receiver_cannot_finalize(tmp_path):
    data = offer(b"abc", tmp_path)
    receiver = FileTransferReceiver(data, tmp_path / "received")
    receiver.write_chunk(0, "YWJj")
    receiver.cancel_event.set()
    with pytest.raises(RuntimeError, match="отменена"):
        receiver.finalize(data["fingerprint"])
    assert not receiver.part_path.exists() and not receiver.final_path.exists()


def test_changed_source_never_reports_complete(tmp_path):
    path = tmp_path / "clip"
    path.write_bytes(b"abc")
    media = MediaInfo("clip", 3, 1, calculate_fingerprint(path))
    sent = []
    sender = FileTransferSender(path, media, lambda kind, payload: sent.append(kind))
    path.write_bytes(b"abd")
    with pytest.raises(ValueError, match="изменился"):
        sender.run()
    assert "file_error" in sent and "file_complete" not in sent


@pytest.mark.parametrize("name", [r"C:\directory\CON.mp4", "../NUL", "a" * 400, "видео" * 100])
def test_windows_filename_is_safe_and_bounded(name):
    result = safe_filename(name)
    assert "/" not in result and "\\" not in result
    assert len(result.encode("utf-8")) <= 180
    assert result.split(".")[0].upper() not in {"CON", "NUL"}


def test_reserved_payload_cannot_override_envelope():
    original = Message("ping", {"type": "play", "session_id": "wrong", "protocol_version": 1}, session_id="room")
    decoded = decode_message(encode_message(original))
    assert decoded.type == "ping" and decoded.session_id == "room" and decoded.protocol_version == PROTOCOL_VERSION


@pytest.mark.parametrize("field,value", [("sent_at", float("nan")), ("sent_at", float("inf")),
                                         ("protocol_version", True), ("message_id", ""), ("type", 123)])
def test_invalid_envelope_is_rejected(field, value):
    data = Message("ping").to_dict()
    data[field] = value
    with pytest.raises(ValueError):
        decode_message(json.dumps(data))


@pytest.mark.parametrize("kind,payload", [("state", {"position_ms": -1, "playing": True, "last_command_id": 0}),
                                          ("ready", {"media_loaded": "false"}),
                                          ("file_offer", {"transfer_id": "t", "file_name": "clip", "file_size": -1, "fingerprint": "x"}),
                                          ("pause", {"command_id": 1, "position_ms": float("nan")})])
def test_invalid_payload_is_rejected(kind, payload):
    with pytest.raises(ValueError):
        decode_message(encode_message(Message(kind, payload)))


@pytest.mark.parametrize("kind,payload", [
    ("media_info", {"file_name": "clip", "file_size": 123, "fingerprint": "x"}),
    ("play", {"command_id": 1, "position_ms": 0}),
])
def test_incomplete_messages_cannot_reach_ui(kind, payload):
    with pytest.raises(ValueError):
        decode_message(encode_message(Message(kind, payload)))


@pytest.mark.parametrize("kind,payload", [
    ("seek_ack", {"seek_id": 1, "media_fingerprint": "x", "position_ms": 5000, "paused": "true"}),
    ("seek_ack", {"seek_id": 1, "media_fingerprint": "x", "paused": True}),
    ("seek_complete", {"seek_id": 0, "media_fingerprint": "x"}),
    ("seek_failed", {"seek_id": 1, "media_fingerprint": "x"}),
])
def test_invalid_seek_confirmation_is_rejected(kind, payload):
    with pytest.raises(ValueError):
        decode_message(encode_message(Message(kind, payload)))


def test_previous_protocol_cannot_join_confirmation_session():
    with pytest.raises(ValueError, match="Несовместимая"):
        decode_message(encode_message(Message("join", {"room_code": "123456"}, protocol_version=2)))
