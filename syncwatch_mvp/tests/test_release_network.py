from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
import websockets

from src.network.connection_manager import ConnectionManager
from src.network.messages import Message
from src.network.protocol import decode_message, encode_message


@asynccontextmanager
async def host_room():
    inbox = asyncio.Queue()
    async def receive(message):
        await inbox.put(message)
    host = ConnectionManager(receive, lambda text: None)
    await host.host(0)
    port = host.server.sockets[0].getsockname()[1]
    try:
        yield host, inbox, f"ws://127.0.0.1:{port}"
    finally:
        await host.close()


async def joined(uri, host, name):
    socket = await websockets.connect(uri, proxy=None)
    await socket.send(encode_message(Message("join", {"room_code": host.room_code, "device_name": name})))
    reply = decode_message(await asyncio.wait_for(socket.recv(), 2))
    assert reply.type == "join_accepted"
    return socket, reply.payload["peer_id"]


async def event(inbox, kind):
    while True:
        message = await asyncio.wait_for(inbox.get(), 2)
        if message.type == kind:
            return message


def test_sequential_three_peers_targeted_send_and_disconnect():
    async def scenario():
        async with host_room() as (host, inbox, uri):
            peers = [await joined(uri, host, name) for name in ("one", "two", "three")]
            try:
                assert len(host.clients) == 3
                await host.send("pause", {"command_id": 1, "position_ms": 42, "execute_delay_ms": 350, "execute_at": 100.0, "media_fingerprint": "x"})
                for socket, peer_id in peers:
                    assert decode_message(await asyncio.wait_for(socket.recv(), 2)).type == "pause"
                await host.send("file_offer", {"transfer_id": "t", "file_name": "movie", "file_size": 100,
                                               "fingerprint": "x"}, peers[1][1])
                assert decode_message(await asyncio.wait_for(peers[1][0].recv(), 2)).type == "file_offer"
                for index in (0, 2):
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(peers[index][0].recv(), 0.02)
                await peers[1][0].close()
                left = await event(inbox, "peer_left")
                assert left.sender_id == peers[1][1] and len(host.clients) == 2
                await host.send("ready", {"media_loaded": True})
                for index in (0, 2):
                    assert decode_message(await asyncio.wait_for(peers[index][0].recv(), 2)).type == "ready"
            finally:
                await asyncio.gather(*(socket.close() for socket, _ in peers))
    asyncio.run(scenario())


def test_ping_reply_is_personal_and_peer_cannot_control_host():
    async def scenario():
        async with host_room() as (host, inbox, uri):
            first, id1 = await joined(uri, host, "one")
            second, id2 = await joined(uri, host, "two")
            try:
                await first.send(encode_message(Message("ping", session_id=host.session_id)))
                assert decode_message(await asyncio.wait_for(first.recv(), 2)).type == "pong"
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(second.recv(), 0.02)
                await first.send(encode_message(Message("play", {"command_id": 1, "position_ms": 0, "execute_delay_ms": 350, "execute_at": 100.0, "media_fingerprint": "x"}, session_id=host.session_id)))
                await first.send(encode_message(Message("seek", {"command_id": 2, "position_ms": 5000,
                    "execute_delay_ms": 350, "execute_at": 100.0, "media_fingerprint": "x"}, session_id=host.session_id)))
                await first.send(encode_message(Message("file_offer", {"transfer_id": "forbidden",
                    "file_name": "other.mp4", "file_size": 100, "fingerprint": "x"}, session_id=host.session_id)))
                await first.send(encode_message(Message("seek_complete", {"seek_id": 1,
                    "media_fingerprint": "x"}, session_id=host.session_id)))
                await first.send(encode_message(Message("ready", {"media_loaded": True}, session_id=host.session_id)))
                message = await event(inbox, "ready")
                assert message.sender_id == id1
                assert not any(item.type in {"play", "seek", "file_offer", "seek_complete"} for item in list(inbox._queue))
            finally:
                await first.close()
                await second.close()
    asyncio.run(scenario())


def test_close_frees_port_and_new_room_has_new_identity():
    async def scenario():
        async with host_room() as (host, inbox, uri):
            socket, peer_id = await joined(uri, host, "one")
            session = host.session_id
            port = host.server.sockets[0].getsockname()[1]
            await host.close()
            await asyncio.wait_for(socket.wait_closed(), 2)
            assert not host.connected
            await host.host(port)
            assert host.session_id != session
            socket2, _ = await joined(uri, host, "two")
            await socket2.close()
    asyncio.run(scenario())


def test_client_failed_join_leaves_no_socket():
    async def scenario():
        async with host_room() as (host, inbox, uri):
            async def receive(message):
                pass
            client = ConnectionManager(receive, lambda text: None)
            with pytest.raises(ConnectionError, match="код"):
                await client.connect("127.0.0.1", int(uri.rsplit(":", 1)[1]), "bad", "name")
            assert client.websocket is None and not client.connected
            await client.close()
    asyncio.run(scenario())


def test_malformed_peer_is_closed_without_interrupting_other_peer():
    async def scenario():
        async with host_room() as (host, inbox, uri):
            first, _ = await joined(uri, host, "one")
            second, id2 = await joined(uri, host, "two")
            try:
                await first.send(encode_message(Message("state", {"position_ms": "oops", "playing": True,
                                                                   "last_command_id": 0}, session_id=host.session_id)))
                await asyncio.wait_for(first.wait_closed(), 2)
                await second.send(encode_message(Message("ready", {"media_loaded": True}, session_id=host.session_id)))
                assert (await event(inbox, "ready")).sender_id == id2
            finally:
                await second.close()
    asyncio.run(scenario())
