from __future__ import annotations

import hashlib
import threading
from concurrent.futures import CancelledError
from pathlib import Path

import pytest
from conftest import wait_until

from src.media.fingerprint import BLOCK_SIZE, calculate_fingerprint
from src.media.local_check import HASH_BATCH_SIZE, LocalFileCheck
from src.media.metadata import MediaInfo
from src.network.messages import Message
from src.network.protocol import decode_message, encode_message
from src.session.role import Role


def check_files(tmp_path, data, candidate=None):
    source, local = tmp_path / "host.mp4", tmp_path / "different-name.mkv"
    source.write_bytes(data)
    local.write_bytes(data if candidate is None else candidate)
    blocks = []
    fingerprint = calculate_fingerprint(source, block_hashes=blocks)
    expected = MediaInfo(source.name, len(data), 60000, fingerprint)
    requests, progress = [], []

    def request(payload):
        requests.append(payload)
        start, count = payload["offset"], payload["count"]
        job.accept_reply({**payload, "hashes": [block.hex() for block in blocks[start:start + count]]})

    job = LocalFileCheck(local, expected, threading.Event(), request, progress.append)
    return job, requests, progress


@pytest.mark.parametrize("different_block", [0, 1, 2])
def test_mismatch_stops_at_first_different_block(tmp_path, monkeypatch, different_block):
    data = b"a" * BLOCK_SIZE * 3
    candidate = bytearray(data)
    candidate[different_block * BLOCK_SIZE + 17] = ord("b")
    job, requests, progress = check_files(tmp_path, data, candidate)
    real_open = Path.open
    reads = []

    class Reader:
        def __enter__(self):
            self.file = real_open(job.path, "rb")
            return self
        def __exit__(self, *args):
            self.file.close()
        def read(self, size):
            reads.append(size)
            return self.file.read(size)

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Reader())
    with pytest.raises(ValueError, match=f"блоке {different_block + 1}"):
        job.run()
    assert reads == [BLOCK_SIZE] * (different_block + 1)
    assert len(requests) == 1 and 100 not in progress


def test_size_mismatch_does_not_read_or_request_hashes(tmp_path, monkeypatch):
    job, requests, progress = check_files(tmp_path, b"video", b"different-size")
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Different-size file was read"))
    with pytest.raises(ValueError, match="размер"):
        job.run()
    assert not requests and not progress


def test_identical_contents_with_different_names_and_partial_last_block(tmp_path):
    job, requests, progress = check_files(tmp_path, b"a" * BLOCK_SIZE + b"tail")
    result = job.run()
    assert result.fingerprint == job.expected.fingerprint
    assert result.file_name != job.expected.file_name
    assert progress[0] == 0 and progress[-1] == 100
    assert requests[0]["count"] == 2


def test_hashes_are_requested_in_bounded_batches(tmp_path, monkeypatch):
    from src.media import local_check

    monkeypatch.setattr(local_check, "BLOCK_SIZE", 8)
    data = b"a" * 8 * (HASH_BATCH_SIZE + 1)
    path = tmp_path / "movie"
    path.write_bytes(data)
    requests = []

    def request(payload):
        requests.append(payload)
        job.accept_reply({**payload, "hashes": [hashlib.sha256(b"a" * 8).hexdigest()] * payload["count"]})

    job = LocalFileCheck(path, MediaInfo("host", len(data), 0, hashlib.sha256(data).hexdigest()),
                         threading.Event(), request, lambda *args: None)
    job.run()
    assert [(p["offset"], p["count"]) for p in requests] == [(0, HASH_BATCH_SIZE), (HASH_BATCH_SIZE, 1)]


def test_waiting_for_host_can_be_cancelled(tmp_path):
    job, _, _ = check_files(tmp_path, b"video")
    job.request = lambda payload: job.cancel_event.set()
    with pytest.raises(CancelledError):
        job.run()


def test_missing_host_reply_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr("src.media.local_check.CHECK_REPLY_TIMEOUT", 0.01)
    job, _, _ = check_files(tmp_path, b"video")
    job.request = lambda payload: None
    with pytest.raises(TimeoutError, match="не ответил"):
        job.run()


def test_old_check_wrong_fingerprint_and_wrong_offset_replies_are_ignored(tmp_path):
    job, _, _ = check_files(tmp_path, b"video")
    def request(payload):
        for key, wrong in (("check_id", "old"), ("media_fingerprint", "old"), ("offset", 1)):
            job.accept_reply({**payload, key: wrong, "reason": "Wrong reply"})
        job.accept_reply({**payload, "hashes": [hashlib.sha256(b"video").hexdigest()]})
    job.request = request
    assert job.run().fingerprint == job.expected.fingerprint


def test_full_digest_is_checked_even_if_block_hashes_match(tmp_path):
    job, _, _ = check_files(tmp_path, b"video")
    job.expected.fingerprint = "wrong-full-digest"
    with pytest.raises(ValueError, match="полная контрольная сумма"):
        job.run()


def test_host_error_does_not_read_candidate(tmp_path, monkeypatch):
    job, _, _ = check_files(tmp_path, b"video")
    job.request = lambda payload: job.accept_reply({**payload, "reason": "Ведущий сменил видео"})
    original = Path.open
    class Reader:
        def __enter__(self):
            self.file = original(job.path, "rb")
            return self
        def __exit__(self, *args):
            self.file.close()
        def read(self, size):
            pytest.fail("Candidate contents were read despite host error")
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Reader())
    with pytest.raises(ValueError, match="сменил"):
        job.run()


def test_host_filename_is_announced_before_hashing_finishes(window_factory, qt_app, tmp_path, monkeypatch):
    host = window_factory()
    host.state.role = Role.HOST
    host.state.connected = True
    path = tmp_path / "expected.mp4"
    path.write_bytes(b"video")
    entered = threading.Event()
    def slow_hash(path, *, cancel_event, block_hashes=None):
        entered.set()
        assert cancel_event.wait(2)
        raise CancelledError()
    monkeypatch.setattr("src.ui.main_window.calculate_fingerprint", slow_hash)
    host._start_media_load(path)
    assert entered.wait(1)
    selected = [call for call in host.network.send.call_args_list if call.args[0] == "media_selected"]
    assert selected and selected[0].args[1]["file_name"] == path.name
    assert not host.state.local_ready
    host.close()


def test_client_sees_selected_name_before_host_finishes_loading(window_factory):
    from src.session.state import PeerState

    client = window_factory()
    client.state.role = Role.CLIENT
    client.state.connected = True
    client.state.session_id = "room"
    client.peers["host"] = PeerState("Host")
    client._on_message(Message("media_selected", {"file_name": "expected.mp4"}, session_id="room", sender_id="host"))
    assert "expected.mp4" in client.host_media_label.text()
    assert not client.choose_media_button.isEnabled()


@pytest.mark.parametrize("kind,fields", [
    ("media_check_request", {"count": 0}), ("media_check_request", {"count": HASH_BATCH_SIZE + 1}),
    ("media_check_request", {"offset": -1}), ("media_check_request", {"count": True}),
    ("media_check_reply", {"hashes": []}), ("media_check_reply", {"hashes": ["invalid"]}),
    ("media_check_reply", {"hashes": ["0" * 64] * (HASH_BATCH_SIZE + 1)}),
    ("media_check_reply", {"reason": "Error", "hashes": ["0" * 64]}),
])
def test_invalid_check_messages_are_rejected(kind, fields):
    payload = {"check_id": "id", "media_fingerprint": "fp", "offset": 0, "count": 1}
    with pytest.raises(ValueError):
        decode_message(encode_message(Message(kind, {**payload, **fields})))


@pytest.mark.parametrize("action", ["cancel", "leave", "replace", "close", "not_ready"])
def test_pending_local_check_is_cancelled_and_cannot_open_video(window_factory, qt_app, tmp_path, action):
    from src.session.state import PeerState

    window = window_factory()
    window.state.role = Role.CLIENT
    window.state.connected = True
    window.state.session_id = "room"
    expected = MediaInfo("expected.mp4", 5, 60000, hashlib.sha256(b"video").hexdigest())
    window.remote_media = expected
    window.peers["host"] = PeerState("Host", expected, True)
    path = tmp_path / "local"
    path.write_bytes(b"video")
    window._start_media_load(path)
    wait_until(qt_app, lambda: any(call.args[0] == "media_check_request" for call in window.network.send.call_args_list))
    generation = window._load_generation
    if action == "cancel":
        window._cancel_local_check()
    elif action == "leave":
        window._go_home()
    elif action == "close":
        window.close()
    elif action == "not_ready":
        window._on_message(Message("not_ready", {"media_loaded": False}, session_id="room", sender_id="host"))
    else:
        replacement = MediaInfo("new.mp4", 5, 60000, "new")
        window._on_message(Message("media_info", replacement.to_dict(), session_id="room", sender_id="host"))
    window._local_media_verified((generation, expected))
    assert not window.player.opened and not window.state.local_ready
    assert window._local_check is None


def test_two_clients_choose_local_files_then_play_and_seek(window_factory, qt_app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from test_release_integration import connected_windows

    host, clients, movie = connected_windows(window_factory, qt_app, tmp_path)
    local = tmp_path / "my-copy.mkv"
    local.write_bytes(movie.read_bytes())
    dialogs = []
    def choose(*args):
        dialogs.append(args[1])
        return str(local), ""
    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    for client in clients:
        wait_until(qt_app, lambda: client.remote_media is not None and client.peers["host"].ready)
        assert movie.name in client.host_media_label.text()
        assert client.choose_media_button.isEnabled()
        client._choose_media()
    wait_until(qt_app, lambda: host._playback_allowed() and all(c._playback_allowed() for c in clients))
    assert all(movie.name in title for title in dialogs)
    assert all(c.local_media.file_name == local.name and c.player.opened == [str(local)] for c in clients)
    assert all(not c.transfer_active for c in clients)
    host._host_play()
    wait_until(qt_app, lambda: host.player.is_playing() and all(c.player.is_playing() for c in clients))
    host._seek_relative(5000)
    wait_until(qt_app, lambda: host._seek is None and host.player.is_playing() and all(c.player.is_playing() for c in clients))
    assert all(abs(host.player.get_position_ms() - c.player.get_position_ms()) < 100 for c in clients)


def test_wrong_local_file_is_rejected_then_correct_file_can_be_selected(window_factory, qt_app, tmp_path):
    from test_release_integration import connected_windows

    host, clients, movie = connected_windows(window_factory, qt_app, tmp_path, count=1)
    client = clients[0]
    wait_until(qt_app, lambda: client.remote_media is not None and client.peers["host"].ready)
    wrong = tmp_path / "same-name.mp4"
    wrong.write_bytes(b"x" * movie.stat().st_size)
    client._start_media_load(wrong)
    wait_until(qt_app, lambda: bool(client.test_errors))
    assert "Файлы разные" in client.test_errors[-1]
    assert not client.state.local_ready and not client.player.opened and not host._playback_allowed()
    client._start_media_load(movie)
    wait_until(qt_app, lambda: client.state.local_ready and host._playback_allowed())


def test_host_file_change_after_index_is_built_rejects_local_check(window_factory, qt_app, tmp_path):
    from test_release_integration import connected_windows

    host, clients, movie = connected_windows(window_factory, qt_app, tmp_path, count=1)
    client = clients[0]
    wait_until(qt_app, lambda: client.remote_media is not None and client.peers["host"].ready)
    local = tmp_path / "old-copy"
    local.write_bytes(movie.read_bytes())
    movie.write_bytes(b"changed")
    client._start_media_load(local)
    wait_until(qt_app, lambda: bool(client.test_errors))
    assert "Файл ведущего изменился" in client.test_errors[-1]
    assert not client.state.local_ready and not client.player.opened
