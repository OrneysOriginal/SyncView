from __future__ import annotations

import hashlib
import socket

import pytest
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


def test_three_windows_handshake_media_play_seek_pause_and_exit(window_factory, qt_app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    host, clients, movie = connected_windows(window_factory, qt_app, tmp_path)
    monkeypatch.setattr(file_transfer, "received_dir", lambda: tmp_path / "received")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    for index in range(host.recipient_combo.count()):
        host.recipient_combo.setCurrentIndex(index)
        host._start_file_offer()
        wait_until(qt_app, lambda: not host.transfer_active, timeout=5)
    wait_until(qt_app, lambda: host._playback_allowed() and all(client._playback_allowed() for client in clients))
    host._host_play()
    wait_until(qt_app, lambda: host.player.is_playing() and all(client.player.is_playing() for client in clients))
    assert max(abs(host.player.get_position_ms()-client.player.get_position_ms()) for client in clients) < 100
    delayed_client = clients[1]
    delayed_send = delayed_client.network.send
    confirmations = []
    def hold_confirmation(kind, payload=None, **kwargs):
        if kind == "seek_ack":
            confirmations.append(dict(payload))
        else:
            delayed_send(kind, payload, **kwargs)
    monkeypatch.setattr(delayed_client.network, "send", hold_confirmation)
    for _ in range(3):
        host._seek_relative(5000)
    assert not host.player.is_playing()
    wait_until(qt_app, lambda: host._seek.local_ready and len(host._seek.waiting) == 1 and bool(confirmations))
    assert all(not client.player.is_playing() for client in clients)
    assert len({host.player.get_position_ms(), *(client.player.get_position_ms() for client in clients)}) == 1
    delayed_send("seek_ack", confirmations[-1])
    monkeypatch.setattr(delayed_client.network, "send", delayed_send)
    wait_until(qt_app, lambda: not host._pending_command_id and all(not client._pending_command_id for client in clients))
    assert host.player.is_playing() and all(client.player.is_playing() for client in clients)
    assert host.player.get_position_ms() >= 15000
    assert all(abs(host.player.get_position_ms()-client.player.get_position_ms()) < 100 for client in clients)
    assert all(client.seek_feedback.amount_ms == 15000 for client in clients)
    positions = [client.player.get_position_ms() for client in clients]
    for client in clients:
        client._seek_relative(-5000)
        client._choose_media()
        client._start_media_load(movie)
    assert all(client.player.get_position_ms() >= position for client, position in zip(clients, positions))
    assert all(len(client.player.opened) == 1 for client in clients)
    host._host_pause()
    wait_until(qt_app, lambda: not host._pending_command_id and all(not client._pending_command_id for client in clients))
    assert all(not client.player.is_playing() for client in clients)
    assert len({host.player.get_position_ms(), *(client.player.get_position_ms() for client in clients)}) == 1
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"replacement-video")
    host._start_media_load(replacement)
    wait_until(qt_app, lambda: host.state.local_ready and all(client.local_media is None for client in clients))
    assert all(not client._playback_allowed() and not client.state.local_ready for client in clients)
    # Restore readiness using the same user-facing host transfer path.
    for index in range(host.recipient_combo.count()):
        host.recipient_combo.setCurrentIndex(index)
        host._start_file_offer()
        wait_until(qt_app, lambda: not host.transfer_active, timeout=5)
    wait_until(qt_app, lambda: host._playback_allowed() and all(client._playback_allowed() for client in clients))
    clients[0]._go_home()
    wait_until(qt_app, lambda: len(host.peers) == 1)
    assert host._playback_allowed()
    host._go_home()
    wait_until(qt_app, lambda: clients[1].stack.currentWidget() == clients[1].home_page)
    assert not clients[1]._playback_allowed()
    assert not host.test_errors


@pytest.mark.parametrize("ending", ["leave", "close", "abrupt", "silent"])
def test_host_departure_exits_every_client(window_factory, qt_app, tmp_path, monkeypatch, ending):
    from src.network import connection_manager

    if ending == "silent":
        # Exercise real keepalive with short intervals rather than waiting 10 seconds.
        monkeypatch.setattr(connection_manager, "PING_INTERVAL_SECONDS", 0.1)
        monkeypatch.setattr(connection_manager, "PING_TIMEOUT_SECONDS", 0.1)
    host, clients, movie = connected_windows(window_factory, qt_app, tmp_path)
    if ending == "leave":
        host._go_home()
    elif ending == "close":
        host.close()
    else:
        def lose_host():
            for socket in tuple(host.network.manager.clients.values()):
                if ending == "abrupt":
                    socket.transport.abort()
                else:
                    socket.transport.pause_reading()
        host.network.loop.call_soon_threadsafe(lose_host)
    wait_until(qt_app, lambda: all(client.stack.currentWidget() == client.home_page for client in clients), timeout=5)
    assert all(client.state.role == Role.NONE and not client.state.connected and not client.peers for client in clients)
    assert all(client.local_media is None and not client.player.is_playing() for client in clients)
    assert all("вышли из комнаты" in client.home_notice.text() for client in clients)
    if ending in {"leave", "close"}:
        assert all("завершил сессию" in client.home_notice.text() for client in clients)
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


def test_custom_room_settings_busy_port_retry_and_two_clients(window_factory, qt_app):
    host = window_factory(real=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        host._show_host_setup()
        host.host_port_edit.setValue(port)
        host.host_code_edit.setText("friends-123")
        host._create_room()
        wait_until(qt_app, lambda: bool(host.test_errors))
        assert "уже занят" in host.test_errors[-1] and str(port) in host.test_errors[-1]
        assert host.state.role == Role.NONE and not host.network.manager.server
        assert host.host_create_button.isEnabled() and host.host_code_edit.text() == "friends-123"
    host._create_room()
    wait_until(qt_app, lambda: bool(host.state.session_id))
    assert host.network.manager.room_code == "friends-123"
    assert f"Порт: {port}" in host.host_info.text()
    clients = []
    for index in range(2):
        client = window_factory(real=True)
        client.state.role = Role.CLIENT
        client.network.connect_to("127.0.0.1", port, "friends-123", f"client{index}")
        clients.append(client)
    wait_until(qt_app, lambda: len(host.peers) == 2 and all(client.state.connected for client in clients))
