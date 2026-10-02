"""Audit regressions. XFAIL marks a confirmed, still unfixed defect.

UI methods are loaded from the actual AST without Qt/VLC imports. This tests
controller logic with fake boundaries, not rendering or real video playback.
"""
from __future__ import annotations

import ast
import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import websockets

from src.media.fingerprint import BLOCK_SIZE, calculate_fingerprint
from src.media.metadata import MediaInfo
from src.network.connection_manager import ConnectionManager
from src.network.file_transfer import FileTransferReceiver, FileTransferSender
from src.network.messages import Message
from src.network.protocol import decode_message, encode_message


def ui_method(name, **namespace):
    path = Path(__file__).parents[1] / "src/ui/main_window.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[method], type_ignores=[])
    scope = {"__name__": __name__, "Message": Message, **namespace}
    exec(compile(module, str(path), "exec"), scope)
    return scope[name]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: pause uses an old position on the client")
def test_pause_ends_at_same_position():
    callbacks = []
    host = SimpleNamespace(
        state=SimpleNamespace(role="host"), player=Mock(), network=Mock(),
        _next_command=lambda: 1, controls_hide_timer=Mock(), _show_player_controls=Mock(),
    )
    host.player.get_position_ms.return_value = 1000
    ui_method("_host_pause", Role=SimpleNamespace(HOST="host"),
              QTimer=SimpleNamespace(singleShot=lambda delay, fn: callbacks.append(fn)))(host)
    # The host keeps playing for 350 ms before its timer fires.
    host.player.get_position_ms.return_value = 1350
    for callback in callbacks:
        callback()
    remote = SimpleNamespace(player=Mock(), controls_hide_timer=Mock(), _show_player_controls=Mock())
    command = host.network.send.call_args.args[1]
    ui_method("_execute_command")(remote, Message("pause", command))
    assert remote.player.seek.call_args.args[0] == host.player.get_position_ms()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: correction target is stale after its delay")
def test_correction_projects_playing_position():
    host = SimpleNamespace(player=Mock(), state=SimpleNamespace(), last_correction=0,
                           _next_command=lambda: 1, network=Mock())
    host.player.get_position_ms.return_value = 1000
    host.player.is_playing.return_value = True
    ui_method("_handle_remote_state", time=SimpleNamespace(monotonic=lambda: 10),
              calculate_drift_ms=lambda a, b: b-a, needs_correction=lambda d: abs(d)>400)(
                  host, Message("state", {"position_ms": 0}))
    payload = host.network.send.call_args.args[1]
    assert payload["position_ms"] >= 1000 + payload["execute_delay_ms"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: returning home leaves the network open")
def test_go_home_closes_room():
    window = SimpleNamespace(transfer_active=False, player=Mock(), state=SimpleNamespace(),
                             network=Mock(), _reset_media_match_state=Mock(),
                             _update_transfer_controls=Mock(), stack=Mock(), home_page=object())
    ui_method("_go_home")(window)
    assert window.network.mock_calls


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: host accepts client playback commands")
def test_host_does_not_schedule_client_playback():
    window = SimpleNamespace(state=SimpleNamespace(role="host"), _schedule_remote_command=Mock())
    ui_method("_on_message", Role=SimpleNamespace(HOST="host"))(window, Message("play"))
    assert not window._schedule_remote_command.called


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: file integrity checks only sampled blocks")
def test_transfer_rejects_corruption_outside_fingerprint_samples(tmp_path):
    source = tmp_path / "movie.bin"
    original = b"a" * (8 * BLOCK_SIZE)
    source.write_bytes(original)
    fingerprint = calculate_fingerprint(source)
    receiver = FileTransferReceiver(
        {"transfer_id": "t", "file_name": source.name, "file_size": len(original),
         "fingerprint": fingerprint}, directory=tmp_path / "received")
    corrupted = bytearray(original)
    corrupted[2 * BLOCK_SIZE] = ord("b")
    for index, start in enumerate(range(0, len(corrupted), BLOCK_SIZE // 2)):
        receiver.write_chunk(index, base64.b64encode(corrupted[start:start+BLOCK_SIZE//2]).decode())
    try:
        result = receiver.finalize(fingerprint)
    except ValueError:
        return
    assert result.read_bytes() == original


def test_receiver_rejects_out_of_order_chunk(tmp_path):
    receiver = FileTransferReceiver(
        {"transfer_id": "t", "file_name": "clip", "file_size": 3, "fingerprint": "x"},
        directory=tmp_path)
    try:
        with pytest.raises(ValueError, match="чанк"):
            receiver.write_chunk(1, "YWJj")
    finally:
        receiver.cleanup()
    assert not receiver.part_path.exists()


def test_sender_cancellation(tmp_path):
    source = tmp_path / "clip"
    source.write_bytes(b"abc")
    send = Mock()
    sender = FileTransferSender(source, MediaInfo("clip", 3, 0, "x"), send)
    sender.cancel()
    sender.run()
    assert [call.args[0] for call in send.call_args_list] == ["file_cancel"]


def test_network_join_and_message_roundtrip():
    async def scenario():
        inbox = asyncio.Queue()
        async def receive(message):
            await inbox.put(message)
        manager = ConnectionManager(receive, lambda status: None)
        try:
            await manager.host(0)
            port = manager.server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as peer:
                await peer.send(encode_message(Message("join", {"room_code": manager.room_code})))
                accepted = decode_message(await asyncio.wait_for(peer.recv(), 2))
                assert accepted.type == "join_accepted"
                assert accepted.session_id == manager.session_id
                await peer.send(encode_message(Message("ready", {"media_loaded": True}, session_id=manager.session_id)))
                assert (await asyncio.wait_for(inbox.get(), 2)).payload["media_loaded"]
                await manager.send("pause", {"position_ms": 42})
                assert decode_message(await asyncio.wait_for(peer.recv(), 2)).payload["position_ms"] == 42
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_network_rejects_wrong_room_code():
    async def scenario():
        async def receive(message):
            pass
        manager = ConnectionManager(receive, lambda status: None)
        try:
            await manager.host(0)
            port = manager.server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as peer:
                await peer.send(encode_message(Message("join", {"room_code": "wrong"})))
                assert decode_message(await asyncio.wait_for(peer.recv(), 2)).type == "join_rejected"
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: session_id is never validated")
def test_network_rejects_wrong_session():
    async def scenario():
        inbox = asyncio.Queue()
        async def receive(message):
            await inbox.put(message)
        manager = ConnectionManager(receive, lambda status: None)
        try:
            await manager.host(0)
            port = manager.server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as peer:
                await peer.send(encode_message(Message("join", {"room_code": manager.room_code})))
                await asyncio.wait_for(peer.recv(), 2)
                await peer.send(encode_message(Message("ready", {"media_loaded": True}, session_id="wrong")))
                try:
                    await asyncio.wait_for(inbox.get(), 0.2)
                except asyncio.TimeoutError:
                    return
                assert False, "Message with a foreign session was forwarded"
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: host commands are not broadcast to all clients")
def test_network_broadcasts_to_multiple_clients():
    async def scenario():
        async def receive(message):
            pass
        manager = ConnectionManager(receive, lambda status: None)
        try:
            await manager.host(0)
            port = manager.server.sockets[0].getsockname()[1]
            uri = f"ws://127.0.0.1:{port}"
            async with websockets.connect(uri, proxy=None) as first, websockets.connect(uri, proxy=None) as second:
                # Both server handlers are now waiting for their join messages.
                join = encode_message(Message("join", {"room_code": manager.room_code}))
                await first.send(join)
                assert decode_message(await asyncio.wait_for(first.recv(), 2)).type == "join_accepted"
                await second.send(join)
                assert decode_message(await asyncio.wait_for(second.recv(), 2)).type == "join_accepted"
                await manager.send("pause", {"position_ms": 42})
                assert decode_message(await asyncio.wait_for(second.recv(), 2)).type == "pause"
                try:
                    message = decode_message(await asyncio.wait_for(first.recv(), 0.2))
                except asyncio.TimeoutError:
                    assert False, "First client did not receive the host command"
                assert message.type == "pause"
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P1: normal disconnect emits no loss status")
def test_host_reports_disconnect():
    async def scenario():
        statuses = []
        async def receive(message):
            pass
        manager = ConnectionManager(receive, statuses.append)
        try:
            await manager.host(0)
            port = manager.server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as peer:
                await peer.send(encode_message(Message("join", {"room_code": manager.room_code})))
                await asyncio.wait_for(peer.recv(), 2)
            # Closing the server waits for its connection handlers to finish.
            await manager.close()
            assert "Соединение потеряно" in statuses
        finally:
            await manager.close()
    asyncio.run(scenario())
