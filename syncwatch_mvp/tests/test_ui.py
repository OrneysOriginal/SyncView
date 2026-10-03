from __future__ import annotations

import time
from pathlib import Path

from conftest import wait_until

from src.media.metadata import MediaInfo
from src.network.messages import Message
from src.session.role import Role
from src.session.state import PeerState


def ready(window, role=Role.HOST, count=1):
    window.state.role = role
    window.state.session_id = "room"
    window.state.connected = True
    window.state.local_ready = True
    window.local_media = MediaInfo("movie.mkv", 100, 60000, "fingerprint")
    window.local_media_path = Path("movie.mkv")
    window.peers = {("host" if role == Role.CLIENT else f"p{i}"): PeerState(f"peer{i}", window.local_media, True)
                    for i in range(count)}
    window.remote_media = window.local_media
    window._check_media_match()
    window.stack.setCurrentWidget(window.player_page)
    return window


def test_pause_ends_at_same_position(window_factory, qt_app):
    host = ready(window_factory())
    host.player.seek(1000)
    host.player.play()
    host._host_pause()
    kind, payload = host.network.send.call_args.args
    client = ready(window_factory(), Role.CLIENT)
    client._on_message(Message(kind, payload, session_id="room", sender_id="host"))
    wait_until(qt_app, lambda: not host._pending_command_id and not client._pending_command_id)
    assert not host.player.is_playing() and not client.player.is_playing()
    assert host.player.get_position_ms() == client.player.get_position_ms()
    assert host.player.get_position_ms() >= 1350


def test_correction_projects_playing_position(window_factory):
    host = ready(window_factory())
    host.player.seek(1000)
    host.player.play()
    host._handle_remote_state(Message("state", {"position_ms": 0, "playing": True, "last_command_id": 0}, sender_id="p0"))
    payload = host.network.send.call_args.args[1]
    assert payload["position_ms"] >= 1000 + payload["execute_delay_ms"]
    assert host.network.send.call_args.kwargs["recipient_id"] == "p0"


def test_go_home_closes_room(window_factory, qt_app):
    window = ready(window_factory())
    window._host_play()
    window._go_home()
    window.network.leave_room.assert_called_once()
    assert not window._command_timers and not window.peers
    assert window.state.role == Role.NONE and not window.state.connected
    assert window.local_media is None
    wait_until(qt_app, lambda: window.stack.currentWidget() == window.home_page)
    assert not window.player.is_playing()


def test_host_does_not_schedule_client_playback(window_factory):
    window = ready(window_factory())
    window._on_message(Message("play", {"command_id": 1, "position_ms": 0}, session_id="room", sender_id="p0"))
    assert not window._command_timers and not window.player.is_playing()


def test_all_peers_must_be_ready(window_factory):
    window = ready(window_factory(), count=3)
    assert window._playback_allowed()
    window._on_message(Message("not_ready", {"media_loaded": False}, session_id="room", sender_id="p1"))
    assert not window._playback_allowed()
    assert window.peers["p0"].ready and window.peers["p2"].ready
    window._on_message(Message("ready", {"media_loaded": True}, session_id="room", sender_id="p1"))
    assert window._playback_allowed()
    window._on_message(Message("peer_left", session_id="room", sender_id="p1"))
    assert window._playback_allowed() and len(window.peers) == 2


def test_latest_command_replaces_pending_command(window_factory, qt_app):
    host = ready(window_factory())
    host._host_play()
    host._host_pause()
    assert len(host._command_timers) == 1
    wait_until(qt_app, lambda: not host._pending_command_id)
    assert not host.player.is_playing()


def test_old_session_command_is_ignored(window_factory):
    client = ready(window_factory(), Role.CLIENT)
    client._on_message(Message("play", {"command_id": 9}, session_id="old", sender_id="host"))
    assert not client._command_timers


def test_stale_media_worker_result_is_ignored(window_factory):
    window = ready(window_factory())
    media = window.local_media
    window._load_generation = 2
    window._media_ready((1, MediaInfo("old", 99, 0, "old"), time.monotonic()+1))
    assert window.local_media is media


def test_failed_decoder_never_becomes_ready(window_factory):
    window = window_factory()
    window.player.error = True
    window._media_ready((0, MediaInfo("bad", 100, 0, "x"), time.monotonic()+1))
    assert not window.state.local_ready and window.local_media is None
    assert "VLC" in window.test_errors[-1]


def test_media_before_join_is_announced_to_new_peer(window_factory):
    host = ready(window_factory())
    host._on_message(Message("peer_joined", {"device_name": "new"}, session_id="room", sender_id="new"))
    sent = [(call.args[0], call.kwargs.get("recipient_id")) for call in host.network.send.call_args_list]
    assert ("media_info", "new") in sent and ("ready", "new") in sent
    assert not host._playback_allowed()


def test_file_offer_is_addressed_to_selected_peer(window_factory):
    host = ready(window_factory(), count=2)
    host.recipient_combo.setCurrentIndex(1)
    host._start_file_offer()
    call = host.network.send.call_args
    assert call.args[0] == "file_offer" and call.kwargs["recipient_id"] == "p1"
    assert host.transfer_active and host.transfer_timeout.isActive()
    host._on_message(Message("file_accept", {"transfer_id": host._transfer_sender.transfer_id},
                             session_id="room", sender_id="p0"))
    assert not host._transfer_running
    host._cancel_file_transfer()
    assert not host.transfer_active and not host.transfer_timeout.isActive()


def test_status_text_does_not_claim_connection(window_factory):
    window = window_factory()
    window._set_status("Подключение…")
    assert not window.state.connected


def test_double_click_does_not_emit_single_click(window_factory):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window = window_factory()
    surface = window.video_surface
    clicks, double_clicks = [], []
    surface.clicked.connect(lambda: clicks.append(True))
    surface.double_clicked.connect(lambda: double_clicks.append(True))
    QTest.mouseDClick(surface, Qt.MouseButton.LeftButton)
    QTest.mouseRelease(surface, Qt.MouseButton.LeftButton)
    QTest.qWait(300)
    assert not clicks and len(double_clicks) == 1
