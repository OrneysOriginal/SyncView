from __future__ import annotations

import time
from pathlib import Path

import pytest
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


def complete_seek(host, qt_app):
    """Simulate each peer's confirmation after the host's own seek settles."""
    wait_until(qt_app, lambda: host._seek is not None and host._seek.local_ready)
    operation = host._seek
    for peer in tuple(operation.waiting):
        host._on_message(Message("seek_ack", {"seek_id": operation.command_id,
            "media_fingerprint": operation.media_fingerprint, "position_ms": operation.position_ms,
            "paused": True}, session_id=host.state.session_id, sender_id=peer))
    wait_until(qt_app, lambda: host._seek is None and not host._pending_command_id)


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


def test_client_cannot_choose_load_send_or_seek(window_factory, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    client = ready(window_factory(), Role.CLIENT)
    client.player.seek(20000)
    media = client.local_media
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: pytest.fail("Client opened file dialog"))
    client._refresh_player_ui()
    client._choose_media()
    client._start_media_load("other.mp4")
    client._start_file_offer()
    client._seek_relative(5000)
    client._timeline_pressed()
    client.timeline.setValue(900)
    client._timeline_released()
    client._broadcast_command("seek", 30000)
    assert client.local_media is media and not client.player.opened
    assert client.player.get_position_ms() == 20000
    assert not client.dragging and not client.transfer_active and not client._command_timers
    client.network.send.assert_not_called()
    assert client.choose_media_button.isHidden() and client.send_file_button.isHidden()
    assert not any(button.isEnabled() for button in
                   (client.timeline, client.seek_back_button, client.seek_forward_button))


@pytest.mark.parametrize("delta,start,expected", [(5000, 10000, 25000), (-5000, 30000, 15000),
                                                (-5000, 1000, 0), (5000, 59000, 59999)])
def test_rapid_seeks_accumulate_and_clamp(window_factory, qt_app, delta, start, expected):
    host = ready(window_factory())
    host.player.seek(start)
    for _ in range(3):
        host._seek_relative(delta)
    assert not host._command_timers and host._seek
    assert host.seek_feedback.amount_ms == delta * 3
    complete_seek(host, qt_app)
    assert host.player.get_position_ms() == expected and not host.player.is_playing()


def test_opposite_seeks_keep_target_and_reset_indicator(window_factory, qt_app):
    host = ready(window_factory())
    host.player.seek(20000)
    host._seek_relative(5000)
    host._seek_relative(5000)
    host._seek_relative(-5000)
    assert host.seek_feedback.amount_ms == -5000
    complete_seek(host, qt_app)
    assert host.player.get_position_ms() == 25000


def test_playing_seek_pauses_until_confirmations_then_resumes(window_factory, qt_app):
    host = ready(window_factory())
    host.player.seek(10000)
    host.player.play()
    host._seek_relative(5000)
    payload = host.network.send.call_args.args[1]
    assert 15000 <= payload["position_ms"] <= 15010
    assert payload["execute_delay_ms"] == 0 and not host.player.is_playing()
    host._seek_relative(5000)
    complete_seek(host, qt_app)
    assert host.player.is_playing() and 20000 <= host.player.get_position_ms() <= 20100


def test_pause_while_seek_pending_preserves_seek_target(window_factory, qt_app):
    host = ready(window_factory())
    host.player.seek(10000)
    host.player.play()
    host._seek_relative(5000)
    host._host_pause()
    complete_seek(host, qt_app)
    assert not host.player.is_playing() and 15000 <= host.player.get_position_ms() <= 15010


@pytest.mark.parametrize("role", [Role.HOST, Role.CLIENT])
def test_focused_slider_keys_seek_only_on_host(window_factory, qt_app, role):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window = ready(window_factory(), role)
    window.player.seek(20000)
    window._refresh_player_ui()
    for key in (Qt.Key.Key_Right, Qt.Key.Key_L, Qt.Key.Key_J):
        QTest.keyClick(window.timeline, key)
    if role == Role.HOST:
        complete_seek(window, qt_app)
    assert window.player.get_position_ms() == (25000 if role == Role.HOST else 20000)
    if role == Role.CLIENT:
        window.network.send.assert_not_called()


def test_timeline_groove_click_seeks_while_paused(window_factory, qt_app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    host = ready(window_factory())
    host.show()
    qt_app.processEvents()
    host._refresh_player_ui()
    QTest.mouseClick(host.timeline, Qt.MouseButton.LeftButton,
                     pos=QPoint(host.timeline.width() * 3 // 4, host.timeline.height() // 2))
    assert host._pending_command_id
    complete_seek(host, qt_app)
    assert 42000 <= host.player.get_position_ms() <= 48000
    assert not host.player.is_playing() and not host.dragging


def test_timeline_drag_keeps_playing_and_reaches_end(window_factory, qt_app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    host = ready(window_factory())
    host.show()
    qt_app.processEvents()
    host.player.play()
    host._refresh_player_ui()
    QTest.mousePress(host.timeline, Qt.MouseButton.LeftButton, pos=QPoint(10, host.timeline.height() // 2))
    assert host.dragging
    QTest.mouseMove(host.timeline, QPoint(host.timeline.width()-1, host.timeline.height() // 2))
    QTest.mouseRelease(host.timeline, Qt.MouseButton.LeftButton,
                       pos=QPoint(host.timeline.width()-1, host.timeline.height() // 2))
    assert not host.dragging
    assert host.network.send.call_args.args[1]["position_ms"] == 59999
    complete_seek(host, qt_app)
    assert host.player.is_playing()


def test_quick_seek_buttons_use_five_seconds(window_factory, qt_app):
    host = ready(window_factory())
    host.player.seek(20000)
    host._refresh_player_ui()
    host.seek_back_button.click()
    assert host.network.send.call_args.args[1]["position_ms"] == 15000
    host.seek_forward_button.click()
    assert host.network.send.call_args.args[1]["position_ms"] == 20000
    complete_seek(host, qt_app)
    assert host.player.get_position_ms() == 20000


def test_client_receives_host_seek_and_indicator(window_factory, qt_app):
    host = ready(window_factory())
    client = ready(window_factory(), Role.CLIENT)
    host._seek_relative(-5000)
    payload = host.network.send.call_args.args[1]
    payload["position_ms"] = 15000
    client._on_message(Message("seek", payload, session_id="room", sender_id="host"))
    wait_until(qt_app, lambda: client._seek and client._seek.local_ready)
    assert client.player.get_position_ms() == 15000
    assert client.seek_feedback.amount_ms == -5000
    assert client.network.send.call_args.args[0] == "seek_ack"
    client._on_message(Message("seek_complete", {"seek_id": payload["command_id"],
                       "media_fingerprint": "fingerprint"}, session_id="room", sender_id="host"))
    assert client._seek is None and not client._pending_command_id


def test_host_media_change_invalidates_client_media_and_pending_load(window_factory):
    client = ready(window_factory(), Role.CLIENT)
    generation = client._load_generation
    media = MediaInfo("new.mp4", 200, 60000, "new-fingerprint")
    client._on_message(Message("media_info", media.to_dict(), session_id="room", sender_id="host"))
    assert client.local_media is None and client.local_media_path is None
    assert not client.state.local_ready and client._load_generation > generation
    client._media_ready((generation, media, time.monotonic()+1))
    assert not client.state.local_ready


def test_wrong_received_file_never_becomes_ready(window_factory):
    client = ready(window_factory(), Role.CLIENT)
    client._media_ready((0, MediaInfo("wrong", 100, 60000, "wrong"), time.monotonic()+1))
    assert not client.state.local_ready and client.local_media is None
    assert "не совпадает" in client.test_errors[-1]


@pytest.mark.parametrize("role,peer,fingerprint", [(Role.HOST, "p0", "fingerprint"),
                                                (Role.CLIENT, "host", "wrong")])
def test_unauthorized_file_offer_is_rejected(window_factory, monkeypatch, role, peer, fingerprint):
    from PySide6.QtWidgets import QMessageBox

    window = ready(window_factory(), role)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("Unauthorized offer shown"))
    window._on_message(Message("file_offer", {"transfer_id": "t", "file_name": "movie",
                       "file_size": 100, "fingerprint": fingerprint}, session_id="room", sender_id=peer))
    assert window.network.send.call_args.args[0] == "file_reject"
    assert not window.transfer_active and not window.player.opened


def test_seek_overlay_expires_and_is_cleared_on_exit(window_factory, qt_app):
    host = ready(window_factory())
    host.show()
    qt_app.processEvents()
    host._seek_relative(5000)
    assert host.seek_feedback.isVisible()
    wait_until(qt_app, lambda: not host.seek_feedback.isVisible())
    host._seek_relative(-5000)
    assert host.seek_feedback.isVisible()
    host._go_home()
    assert not host.seek_feedback.isVisible()


def seek_ack(host, peer="p0", **changes):
    operation = host._seek
    payload = {"seek_id": operation.command_id, "media_fingerprint": operation.media_fingerprint,
               "position_ms": operation.position_ms, "paused": True}
    payload.update(changes)
    return Message("seek_ack", payload, session_id=host.state.session_id, sender_id=peer)


def test_seek_waits_for_every_peer_without_extra_key_press(window_factory, qt_app):
    host = ready(window_factory(), count=2)
    host.player.play()
    host._seek_relative(5000)
    assert not host.player.is_playing()
    wait_until(qt_app, lambda: host._seek.local_ready)
    host._on_message(seek_ack(host, "p0"))
    assert not host.player.is_playing() and host._seek.waiting == {"p1"}
    assert not any(call.args[0] == "play" for call in host.network.send.call_args_list)
    host._on_message(seek_ack(host, "p1"))
    wait_until(qt_app, lambda: host.player.is_playing())
    assert host._seek is None
    assert [call.args[0] for call in host.network.send.call_args_list] == ["seek", "seek_complete", "play"]


def test_seek_waits_for_local_player_as_well(window_factory, qt_app):
    host = ready(window_factory())
    host.player.play()
    host._seek_relative(5000)
    target = host._seek.position_ms
    host.player.position = 0  # Asynchronous VLC seek has not settled yet.
    host._on_message(seek_ack(host))
    host._poll_seek_position()
    assert host._seek and not host._seek.local_ready and not host.player.is_playing()
    host.player.position = target
    wait_until(qt_app, lambda: host.player.is_playing())


@pytest.mark.parametrize("changes", [{"seek_id": 999}, {"media_fingerprint": "old"},
                                     {"position_ms": 50000}, {"paused": False}])
def test_invalid_confirmation_cannot_resume_playback(window_factory, qt_app, changes):
    host = ready(window_factory())
    host.player.play()
    host._seek_relative(5000)
    wait_until(qt_app, lambda: host._seek.local_ready)
    host._on_message(seek_ack(host, **changes))
    assert host._seek.waiting == {"p0"} and not host.player.is_playing()


def test_duplicate_and_previous_seek_ack_cannot_confirm_latest_seek(window_factory, qt_app):
    host = ready(window_factory(), count=2)
    host.player.play()
    host._seek_relative(5000)
    old = seek_ack(host)
    host._seek_relative(5000)
    wait_until(qt_app, lambda: host._seek.local_ready)
    host._on_message(old)
    host._on_message(seek_ack(host))
    host._on_message(seek_ack(host))
    assert host._seek.waiting == {"p1"} and not host.player.is_playing()
    host._on_message(seek_ack(host, "p1"))
    wait_until(qt_app, lambda: host.player.is_playing())
    assert 10000 <= host.player.get_position_ms() <= 10100


def test_client_confirms_only_after_position_and_pause_settle(window_factory, qt_app):
    host = ready(window_factory())
    client = ready(window_factory(), Role.CLIENT)
    client.player.play()
    host._seek_relative(5000)
    payload = host.network.send.call_args.args[1]
    client._on_message(Message("seek", payload, session_id="room", sender_id="host"))
    client.player.position = 0
    client._poll_seek_position()
    client.network.send.assert_not_called()
    assert not client.player.is_playing()
    client.player.position = payload["position_ms"]
    wait_until(qt_app, lambda: client._seek.local_ready)
    assert client.network.send.call_args.args == ("seek_ack", {
        "seek_id": payload["command_id"], "position_ms": payload["position_ms"],
        "paused": True, "media_fingerprint": "fingerprint"})
    assert not client.player.is_playing() and client._seek


def test_timeout_keeps_pause_and_play_retries_confirmations(window_factory, qt_app):
    host = ready(window_factory())
    host.player.play()
    host._seek_relative(5000)
    wait_until(qt_app, lambda: host._seek.local_ready)
    old_id = host._seek.command_id
    host.seek_timeout.start(1)
    wait_until(qt_app, lambda: host._seek.failed)
    assert not host.player.is_playing() and not host.seek_poll.isActive() and not host.seek_timeout.isActive()
    assert "peer0" in host.player_status.text() and "паузе" in host.player_status.text()
    assert host.network.send.call_args.args[0] == "seek_failed"
    host._on_message(seek_ack(host))
    assert not host.player.is_playing()
    host._toggle_playback()
    assert host._seek.command_id > old_id and not host._seek.failed
    complete_seek(host, qt_app)
    assert host.player.is_playing()


def test_remote_seek_failure_stops_whole_group(window_factory, qt_app):
    host = ready(window_factory(), count=2)
    host.player.play()
    host._seek_relative(5000)
    host._on_message(Message("seek_failed", {"seek_id": host._seek.command_id,
                     "media_fingerprint": "fingerprint", "reason": "Ошибка декодера"},
                     session_id="room", sender_id="p0"))
    assert host._seek.failed and not host.player.is_playing()
    assert host.network.send.call_args.args[0] == "seek_failed"
    host._host_play()
    assert host._seek and not host._seek.failed and not host.player.is_playing()


def test_play_during_seek_does_not_bypass_missing_confirmations(window_factory, qt_app):
    host = ready(window_factory())
    host._seek_relative(5000)
    wait_until(qt_app, lambda: host._seek.local_ready)
    host._host_play()
    assert not host.player.is_playing() and host._seek.resume
    complete_seek(host, qt_app)
    assert host.player.is_playing()


@pytest.mark.parametrize("event", ["exit", "disconnect", "new_peer", "media"])
def test_room_changes_cancel_waiting_seek(window_factory, qt_app, event):
    host = ready(window_factory(), count=2)
    host.player.play()
    host._seek_relative(5000)
    acknowledgement = seek_ack(host)
    if event == "exit":
        host._go_home()
    elif event == "disconnect":
        host._on_message(Message("peer_left", session_id="room", sender_id="p0"))
    elif event == "new_peer":
        host._on_message(Message("peer_joined", {"device_name": "new"}, session_id="room", sender_id="new"))
    else:
        host._on_message(Message("media_info", host.local_media.to_dict(), session_id="room", sender_id="p0"))
    host._on_message(acknowledgement)
    assert host._seek is None and not host.seek_poll.isActive() and not host.seek_timeout.isActive()
    wait_until(qt_app, lambda: not host._pending_command_id)
    assert not host.player.is_playing()


def test_seek_wait_and_failure_remain_visible_in_fullscreen(window_factory, qt_app):
    host = ready(window_factory())
    host._enter_fullscreen()
    host.player.play()
    host._seek_relative(5000)
    assert host.info_card.isHidden() and not host.seek_status_banner.isHidden()
    assert "Синхронизация" in host.seek_status_banner.text()
    host._seek_timed_out()
    assert not host.seek_status_banner.isHidden() and "паузе" in host.seek_status_banner.text()
    host._go_home()
    assert host.seek_status_banner.isHidden()


def test_last_ack_rechecks_host_position_before_resuming(window_factory, qt_app):
    host = ready(window_factory())
    host.player.play()
    host._seek_relative(5000)
    wait_until(qt_app, lambda: host._seek.local_ready)
    target = host._seek.position_ms
    host.player.position = 0
    host._on_message(seek_ack(host))
    assert host._seek and not host._seek.local_ready and host.seek_poll.isActive()
    assert not host.player.is_playing()
    host.player.position = target
    wait_until(qt_app, lambda: host.player.is_playing())


def test_automatic_resume_does_not_seek_again(window_factory, qt_app, monkeypatch):
    from unittest.mock import Mock

    host = ready(window_factory())
    seek = Mock(wraps=host.player.seek)
    monkeypatch.setattr(host.player, "seek", seek)
    host.player.play()
    host._seek_relative(5000)
    complete_seek(host, qt_app)
    assert host.player.is_playing()
    assert seek.call_count == 1


def test_client_timeout_and_late_play_do_not_bypass_failure(window_factory, qt_app):
    host = ready(window_factory())
    client = ready(window_factory(), Role.CLIENT)
    host._seek_relative(5000)
    payload = host.network.send.call_args.args[1]
    client._on_message(Message("seek", payload, session_id="room", sender_id="host"))
    wait_until(qt_app, lambda: client._seek.local_ready)
    client.seek_timeout.start(1)
    wait_until(qt_app, lambda: client._seek.failed)
    assert client.network.send.call_args.args[0] == "seek_failed"
    late_play = {**payload, "command_id": payload["command_id"] + 1, "resume": True}
    client._on_message(Message("play", late_play, session_id="room", sender_id="host"))
    assert not client.player.is_playing() and not client._command_timers
