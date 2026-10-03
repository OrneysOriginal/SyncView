"""Audit regressions. XFAIL marks a confirmed, still unfixed defect.

UI methods are loaded from the actual AST without Qt/VLC imports. This tests
controller logic with fake boundaries, not rendering or real video playback.
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import Mock

import pytest
import websockets

from src.media.fingerprint import BLOCK_SIZE, calculate_fingerprint
from src.media.metadata import MediaInfo
from src.network.connection_manager import ConnectionManager
from src.network.file_transfer import FileTransferReceiver, FileTransferSender
from src.network.messages import Message
from src.network.protocol import decode_message, encode_message


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
            if message.type == "ready":
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
                await manager.send("pause", {"command_id": 1, "position_ms": 42, "execute_delay_ms": 350, "execute_at": 100.0, "media_fingerprint": "x"})
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


def test_network_rejects_wrong_session():
    async def scenario():
        inbox = asyncio.Queue()
        async def receive(message):
            if message.type == "ready":
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
                await manager.send("pause", {"command_id": 1, "position_ms": 42, "execute_delay_ms": 350, "execute_at": 100.0, "media_fingerprint": "x"})
                assert decode_message(await asyncio.wait_for(second.recv(), 2)).type == "pause"
                try:
                    message = decode_message(await asyncio.wait_for(first.recv(), 0.2))
                except asyncio.TimeoutError:
                    assert False, "First client did not receive the host command"
                assert message.type == "pause"
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_host_reports_disconnect():
    async def scenario():
        statuses = []
        disconnected = asyncio.Event()
        async def receive(message):
            if message.type == "peer_left":
                disconnected.set()
        manager = ConnectionManager(receive, statuses.append)
        try:
            await manager.host(0)
            port = manager.server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}", proxy=None) as peer:
                await peer.send(encode_message(Message("join", {"room_code": manager.room_code})))
                await asyncio.wait_for(peer.recv(), 2)
            await asyncio.wait_for(disconnected.wait(), 2)
            await manager.close()
            assert "Соединение потеряно" in statuses
        finally:
            await manager.close()
    asyncio.run(scenario())
