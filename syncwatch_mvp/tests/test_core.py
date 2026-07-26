from pathlib import Path

from src.media.fingerprint import calculate_fingerprint
from src.media.metadata import MediaInfo
from src.media.validator import media_matches
from src.network.file_transfer import CHUNK_SIZE, FileTransferReceiver, FileTransferSender
from src.network.messages import Message
from src.network.protocol import MAX_MESSAGE_SIZE, decode_message, encode_message
from src.synchronization.clock_sync import ClockSample, execution_delay_seconds, summarize
from src.synchronization.drift_calculator import calculate_drift_ms, needs_correction


def test_message_roundtrip() -> None:
    original = Message("play", {"command_id": 1, "position_ms": 42})
    decoded = decode_message(encode_message(original))
    assert decoded.type == "play"
    assert decoded.payload["command_id"] == 1


def test_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "video.bin"
    path.write_bytes(b"abc" * 2_000_000)
    assert calculate_fingerprint(path) == calculate_fingerprint(path)


def test_media_match() -> None:
    left = MediaInfo("a.mkv", 100, 10_000, "x")
    right = MediaInfo("b.mkv", 100, 10_900, "x")
    assert media_matches(left, right)
    assert not media_matches(left, MediaInfo("b.mkv", 101, 10_000, "x"))


def test_media_force_match_message_roundtrip() -> None:
    original = Message("media_force_match", {})
    decoded = decode_message(encode_message(original))
    assert decoded.type == "media_force_match"


def test_file_offer_message_roundtrip() -> None:
    original = Message(
        "file_offer",
        {
            "transfer_id": "abc",
            "file_name": "movie.mp4",
            "file_size": 123,
            "fingerprint": "deadbeef",
            "duration_ms": 1000,
        },
    )
    decoded = decode_message(encode_message(original))
    assert decoded.type == "file_offer"
    assert decoded.payload["transfer_id"] == "abc"
    assert decoded.payload["file_name"] == "movie.mp4"


def test_message_size_limit_allows_chunk() -> None:
    assert MAX_MESSAGE_SIZE >= 1 * 1024 * 1024
    assert CHUNK_SIZE * 4 // 3 < MAX_MESSAGE_SIZE


def test_file_transfer_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "clip.bin"
    source.write_bytes(bytes((index % 256) for index in range(CHUNK_SIZE * 2 + 123)))
    fingerprint = calculate_fingerprint(source)
    media = MediaInfo(source.name, source.stat().st_size, 5000, fingerprint)

    inbox: list[tuple[str, dict]] = []

    def send(message_type: str, payload: dict) -> None:
        inbox.append((message_type, payload))

    sender = FileTransferSender(source, media, send=send)
    offer = sender.offer_payload()
    receiver = FileTransferReceiver(offer, directory=tmp_path / "recv")
    sender.run()

    assert inbox[-1][0] == "file_complete"
    for message_type, payload in inbox:
        if message_type == "file_chunk":
            receiver.write_chunk(int(payload["index"]), str(payload["data"]))
        elif message_type == "file_complete":
            result = receiver.finalize(str(payload["fingerprint"]))
            assert result.exists()
            assert calculate_fingerprint(result) == fingerprint
            assert result.read_bytes() == source.read_bytes()


def test_clock_summary() -> None:
    rtt, offset = summarize([ClockSample(1.0, 1.1, 1.2), ClockSample(2.0, 2.1, 2.3)])
    assert rtt > 0
    assert abs(offset) < 0.1
    assert execution_delay_seconds(0.02) == 0.3


def test_drift() -> None:
    assert calculate_drift_ms(1000, 1450) == 450
    assert needs_correction(450)
    assert not needs_correction(200)
