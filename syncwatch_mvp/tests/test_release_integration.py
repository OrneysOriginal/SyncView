from __future__ import annotations

import hashlib

from conftest import wait_until

from src.network import file_transfer
from src.session.role import Role


def connected_windows(window_factory, qt_app, tmp_path, count=2):
    movie = tmp_path / "movie.mp4"
    movie.write_bytes(b"test-video" * 80000)
    host = window_factory(real=True)
    host.state.role = Role.HOST
    host.network.host(0)
    wait_until(qt_app, lambda: bool(host.state.session_id))
    port = host.network.manager.server.sockets[0].getsockname()[1]
    host._start_media_load(movie)
    wait_until(qt_app, lambda: host.state.local_ready)
    clients = []
    for index in range(count):
        client = window_factory(real=True)
        client.state.role = Role.CLIENT
        client.network.connect_to("127.0.0.1", port, host.network.manager.room_code, f"client{index}")
        clients.append(client)
    wait_until(qt_app, lambda: len(host.peers) == count and all(client.state.connected for client in clients))
    return host, clients, movie


def test_three_windows_handshake_media_play_seek_pause_and_exit(window_factory, qt_app, tmp_path):
    host, clients, movie = connected_windows(window_factory, qt_app, tmp_path)
    for client in clients:
        client._start_media_load(movie)
    wait_until(qt_app, lambda: host._playback_allowed() and all(client._playback_allowed() for client in clients))
    host._host_play()
    wait_until(qt_app, lambda: host.player.is_playing() and all(client.player.is_playing() for client in clients))
    assert max(abs(host.player.get_position_ms()-client.player.get_position_ms()) for client in clients) < 100
    host._seek_relative(5000)
    wait_until(qt_app, lambda: host.player.get_position_ms() >= 5000 and all(client.player.get_position_ms() >= 5000 for client in clients))
    host._host_pause()
    wait_until(qt_app, lambda: not host._pending_command_id and all(not client._pending_command_id for client in clients))
    assert all(not client.player.is_playing() for client in clients)
    assert len({host.player.get_position_ms(), *(client.player.get_position_ms() for client in clients)}) == 1
    clients[0]._go_home()
    wait_until(qt_app, lambda: len(host.peers) == 1)
    assert host._playback_allowed()
    host._go_home()
    wait_until(qt_app, lambda: not clients[1].state.connected)
    assert not clients[1]._playback_allowed()
    assert not host.test_errors


def test_file_reaches_only_selected_client_and_sha256_is_confirmed(window_factory, qt_app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    host, clients, movie = connected_windows(window_factory, qt_app, tmp_path)
    monkeypatch.setattr(file_transfer, "received_dir", lambda: tmp_path / "received")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    host.recipient_combo.setCurrentIndex(1)
    target_id = host.recipient_combo.currentData()
    target_index = int(host.peers[target_id].name[-1])
    host._start_file_offer()
    wait_until(qt_app, lambda: clients[target_index].state.local_ready and not host.transfer_active, timeout=5)
    target = clients[target_index]
    assert target.local_media.fingerprint == hashlib.sha256(movie.read_bytes()).hexdigest()
    assert target.local_media_path.read_bytes() == movie.read_bytes()
    assert not clients[1-target_index].player.opened
    assert not target.transfer_active and not host.transfer_timeout.isActive()
    assert not host.test_errors
