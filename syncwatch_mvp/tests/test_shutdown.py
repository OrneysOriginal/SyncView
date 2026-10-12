from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import threading
from concurrent.futures import CancelledError
from pathlib import Path

import pytest
from conftest import wait_until

from src.media.fingerprint import BLOCK_SIZE, calculate_fingerprint
from src.network.file_transfer import CHUNK_SIZE, FileTransferReceiver
from src.session.role import Role


def test_fingerprint_stops_reading_on_cancel(tmp_path, monkeypatch):
    movie = tmp_path / "movie"
    movie.write_bytes(b"a" * (3 * BLOCK_SIZE))
    cancel = threading.Event()
    real_open = Path.open
    reads = []

    class Reader:
        def __enter__(self):
            self.file = real_open(movie, "rb")
            return self
        def __exit__(self, *args):
            self.file.close()
        def read(self, size):
            reads.append(size)
            block = self.file.read(size)
            cancel.set()
            return block

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Reader())
    with pytest.raises(CancelledError):
        calculate_fingerprint(movie, cancel_event=cancel)
    assert reads == [BLOCK_SIZE]


def test_receiver_cancels_fallback_copy_and_removes_partial_result(tmp_path, monkeypatch):
    data = b"a" * (2 * CHUNK_SIZE)
    digest = hashlib.sha256(data).hexdigest()
    receiver = FileTransferReceiver({"transfer_id": "t", "file_name": "movie", "file_size": len(data),
                                     "fingerprint": digest}, tmp_path)
    for index in range(2):
        receiver.write_chunk(index, base64.b64encode(data[index * CHUNK_SIZE:(index + 1) * CHUNK_SIZE]))
    real_open = Path.open

    class Reader:
        def __enter__(self):
            self.file = real_open(receiver.part_path, "rb")
            return self
        def __exit__(self, *args):
            self.file.close()
        def read(self, size):
            block = self.file.read(size)
            receiver.cancel_event.set()
            return block

    def no_hard_links(*args):
        raise OSError("No hard links on this filesystem")

    monkeypatch.setattr("src.network.file_transfer.os.link", no_hard_links)
    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs:
                        Reader() if path == receiver.part_path else real_open(path, *args, **kwargs))
    with pytest.raises(RuntimeError, match="отменена"):
        receiver.finalize(digest)
    assert not receiver.part_path.exists() and not receiver.final_path.exists()


@pytest.mark.parametrize("action", ["leave", "replace"])
def test_loading_is_cancelled_when_leaving_or_replacing_video(window_factory, qt_app, tmp_path,
                                                            monkeypatch, action):
    window = window_factory()
    window.state.role = Role.HOST
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    entered, stopped = threading.Event(), threading.Event()

    def slow_hash(path, *, cancel_event, block_hashes=None):
        if Path(path) == first:
            entered.set()
            assert cancel_event.wait(2)
            stopped.set()
            raise CancelledError()
        return calculate_fingerprint(path, cancel_event=cancel_event, block_hashes=block_hashes)

    monkeypatch.setattr("src.ui.main_window.calculate_fingerprint", slow_hash)
    window._start_media_load(first)
    assert entered.wait(1)
    if action == "leave":
        window._go_home()
    else:
        window._start_media_load(second)
        wait_until(qt_app, lambda: window.state.local_ready)
        assert window.local_media.fingerprint == hashlib.sha256(b"second").hexdigest()
    assert stopped.wait(1)


def test_cancelling_send_unblocks_worker_while_network_keeps_running(qt_app):
    import asyncio

    from src.network.network_thread import NetworkThread

    network = NetworkThread()
    entered, cancel = threading.Event(), threading.Event()
    errors = []

    async def blocked_send(*args):
        entered.set()
        await asyncio.Event().wait()

    def work():
        try:
            network.send_wait("file_chunk", cancel_event=cancel)
        except BaseException as exc:
            errors.append(exc)

    network.start()
    worker = threading.Thread(target=work)
    try:
        assert network._ready.wait(1)
        network.manager.send = blocked_send
        worker.start()
        assert entered.wait(1)
        cancel.set()
        worker.join(1)
        assert not worker.is_alive()
        assert network.isRunning()
        assert len(errors) == 1 and isinstance(errors[0], CancelledError)
    finally:
        cancel.set()
        network.shutdown()
        if worker.ident is not None:
            worker.join(1)


# A child interpreter proves that Python actually exits rather than leaving a
# hidden process waiting for ThreadPoolExecutor's interpreter-exit hook.
PROCESS_CHECK = r'''
import faulthandler
faulthandler.dump_traceback_later(4)
import asyncio
import hashlib
import sys
import threading
from concurrent.futures import CancelledError
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

sys.modules["vlc"] = ModuleType("vlc")
from src.ui import main_window
from src.media.metadata import MediaInfo
from src.network.file_transfer import FileTransferSender, FileTransferReceiver
from src.session.role import Role

mode, directory = sys.argv[1], Path(sys.argv[2])
player = Mock()
player.get_duration_ms.return_value = 60000
player.get_position_ms.return_value = 0
player.is_playing.return_value = False
player.is_ready.return_value = True
main_window.VlcPlayer = lambda *args, **kwargs: player
app = QApplication([])
window = main_window.MainWindow()
assert window.network._ready.wait(1)
window.state.role = Role.HOST
movie = directory / "movie"
movie.write_bytes(b"video")
entered = threading.Event()
receiver = None

if mode in {"hash", "quit"}:
    def slow_hash(path, *, cancel_event=None, block_hashes=None):
        entered.set()
        assert (cancel_event or threading.Event()).wait(30), "Hash was never cancelled"
        raise CancelledError()
    main_window.calculate_fingerprint = slow_hash
    window._start_media_load(movie)
elif mode == "local_check":
    from src.session.state import PeerState
    media = MediaInfo("host-video.mp4", 5, 60000, hashlib.sha256(b"video").hexdigest())
    window.state.role = Role.CLIENT
    window.state.connected = True
    window.remote_media = media
    window.peers["host"] = PeerState("Host", media, True)
    async def unanswered_request(kind, *args):
        if kind == "media_check_request":
            entered.set()
    window.network.manager.send = unanswered_request
    window._start_media_load(movie)
elif mode == "send":
    async def blocked_send(*args):
        entered.set()
        await asyncio.Event().wait()
    window.network.manager.send = blocked_send
    media = MediaInfo("movie", 5, 60000, hashlib.sha256(b"video").hexdigest())
    def send(kind, payload):
        window.network.send_wait(kind, payload, cancel_event=sender.cancel_event)
    sender = FileTransferSender(movie, media, send)
    window._transfer_sender = sender
    window.transfer_active = True
    window._begin_outgoing_transfer()
elif mode == "receive":
    receiver = FileTransferReceiver({"transfer_id": "t", "file_name": "received",
        "file_size": 5, "fingerprint": hashlib.sha256(b"video").hexdigest()}, directory)
    window.network.start_receiving(receiver, "peer")
    window._transfer_receiver = receiver
    window.transfer_active = True
    entered.set()
else:
    entered.set()

window.show()
timer = QTimer()
def finish():
    if not entered.is_set():
        return
    timer.stop()
    if mode == "fullscreen":
        window._enter_fullscreen()
    if mode == "quit":
        app.quit()
    else:
        window.close()
timer.timeout.connect(finish)
timer.start(10)
QTimer.singleShot(5000, lambda: app.exit(99))
assert app.exec() == 0
assert window._closing
assert not window.network.isRunning()
assert all(not thread.is_alive() for thread in window.executor._threads)
player.release.assert_called_once()
window.close()
app.processEvents()
player.release.assert_called_once()
if receiver is not None:
    assert not receiver.part_path.exists()
print("PROCESS_EXIT_OK", flush=True)
'''


@pytest.mark.parametrize("mode", ["idle", "hash", "send", "receive", "fullscreen", "quit", "local_check"])
def test_closing_window_exits_process_with_background_work(tmp_path, mode):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen([sys.executable, "-B", "-c", PROCESS_CHECK, mode, str(tmp_path)],
                               cwd=Path(__file__).parents[1], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        output, error = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        output, error = process.communicate()
        pytest.fail(f"Application process did not exit ({mode}): {output}\n{error}")
    assert process.returncode == 0, output + error
    assert "PROCESS_EXIT_OK" in output


def test_window_icon_contains_taskbar_and_high_dpi_sizes(qt_app):
    from src.ui.app_icon import application_icon

    icon = application_icon()
    assert not icon.isNull()
    assert {16, 20, 24, 32, 40, 48, 64, 128, 256} <= {size.width() for size in icon.availableSizes()}
    for size in (16, 32, 256):
        assert not icon.pixmap(size, size).isNull()
