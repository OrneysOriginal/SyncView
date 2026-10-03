from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable

from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from src.network.messages import Message
from src.network.protocol import HOST_COMMANDS, MAX_MESSAGE_SIZE, decode_message, encode_message
from src.version import APP_VERSION

MessageHandler = Callable[[Message], Awaitable[None]]
StatusHandler = Callable[[str], None]


class ConnectionManager:
    def __init__(self, on_message: MessageHandler, on_status: StatusHandler) -> None:
        self.on_message = on_message
        self.on_status = on_status
        self.session_id = secrets.token_hex(16)
        self.room_code = f"{secrets.randbelow(1000000):06d}"
        self.websocket: ClientConnection | None = None
        self.clients: dict[str, ServerConnection] = {}
        self.server = None
        self.is_host = False
        self._room_code_expected = ""
        self._generation = 0
        self._connecting: asyncio.Task | None = None
        self._closing = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return bool(self.clients) if self.is_host else self.websocket is not None

    async def host(self, port: int) -> None:
        async with self._lifecycle_lock:
            await self._close()
            self.is_host = True
            self.session_id = secrets.token_hex(16)
            self.room_code = f"{secrets.randbelow(1000000):06d}"
            self._room_code_expected = self.room_code
            self.server = await serve(self._accept_client, "0.0.0.0", port,
                                      max_size=MAX_MESSAGE_SIZE, max_queue=4,
                                      compression=None, close_timeout=2)
            await self.on_message(Message("host_started", {"room_code": self.room_code}, session_id=self.session_id))
            self.on_status("Сервер запущен. Ожидание участников…")

    async def _accept_client(self, websocket: ServerConnection) -> None:
        peer_id = secrets.token_hex(8)
        generation = self._generation
        accepted = False
        try:
            join = decode_message(await asyncio.wait_for(websocket.recv(), 10))
            if join.type != "join":
                raise ValueError("Ожидался запрос join")
            if join.payload["room_code"] != self._room_code_expected:
                await websocket.send(encode_message(Message("join_rejected", {"reason": "Неверный код"})))
                return
            if generation != self._generation or self._closing:
                return
            self.clients[peer_id] = websocket
            accepted = True
            await websocket.send(encode_message(Message("join_accepted", {"peer_id": peer_id}, session_id=self.session_id)))
            await self.on_message(Message("peer_joined", {"device_name": join.payload.get("device_name", "Участник")},
                                          session_id=self.session_id, sender_id=peer_id))
            self.on_status(f"Подключено участников: {len(self.clients)}")
            await self._listen(websocket, peer_id, generation)
        except (ValueError, asyncio.TimeoutError) as exc:
            logging.warning("Rejected peer: %s", exc)
            await websocket.close(code=1008, reason="Некорректный запрос или протокол")
        except ConnectionClosed:
            pass
        except Exception:
            logging.exception("Client handler failed")
        finally:
            self.clients.pop(peer_id, None)
            await websocket.close()
            if accepted and generation == self._generation:
                await self.on_message(Message("peer_left", session_id=self.session_id, sender_id=peer_id))
                self.on_status(f"Подключено участников: {len(self.clients)}" if self.clients else "Соединение потеряно")

    async def connect(self, ip: str, port: int, room_code: str, device_name: str) -> None:
        # Addresses are literal IPs, preventing URI injection and proxy routing.
        address = ipaddress.ip_address(ip)
        host = f"[{address}]" if address.version == 6 else str(address)
        async with self._lifecycle_lock:
            await self._close()
            generation = self._generation
            self.is_host = False
            self._connecting = asyncio.current_task()
            self.on_status("Подключение…")
        websocket = None
        accepted = False
        try:
            websocket = await connect(f"ws://{host}:{port}", proxy=None, max_size=MAX_MESSAGE_SIZE,
                                      max_queue=4, compression=None, open_timeout=10, close_timeout=2)
            self.websocket = websocket
            await self.send("join", {"room_code": room_code, "device_name": device_name,
                                     "app_version": APP_VERSION})
            first = decode_message(await asyncio.wait_for(websocket.recv(), 10))
            if first.type == "join_rejected":
                raise ConnectionError(str(first.payload.get("reason", "Соединение отклонено")))
            if first.type != "join_accepted" or not first.session_id:
                raise ConnectionError("Некорректный ответ сервера")
            self.session_id = first.session_id
            accepted = True
            self.on_status("Подключено")
            await self.on_message(first)
            await self._listen(websocket, "host", generation)
        finally:
            if websocket is not None:
                await websocket.close()
            if generation == self._generation:
                self.websocket = None
                if accepted:
                    await self.on_message(Message("peer_left", session_id=self.session_id, sender_id="host"))
                    self.on_status("Соединение потеряно")
            if self._connecting is asyncio.current_task():
                self._connecting = None

    async def _listen(self, websocket, peer_id: str, generation: int) -> None:
        seen = deque(maxlen=2048)
        try:
            async for raw in websocket:
                if generation != self._generation:
                    break
                message = decode_message(raw)
                if message.session_id != self.session_id:
                    await websocket.close(code=1008, reason="Чужая сессия")
                    break
                if message.message_id in seen:
                    continue
                seen.append(message.message_id)
                message.sender_id = peer_id
                message.received_at = time.monotonic()
                if self.is_host and message.type in HOST_COMMANDS:
                    logging.warning("Ignored host command from peer %s", peer_id)
                    continue
                if message.type in {"join", "join_accepted", "join_rejected"}:
                    raise ValueError("Повторный handshake")
                if message.type == "ping":
                    pong = Message("pong", {"echo": message.sent_at, "remote_time": time.monotonic()},
                                   session_id=self.session_id)
                    await websocket.send(encode_message(pong))
                else:
                    await self.on_message(message)
        except ValueError as exc:
            logging.warning("Invalid message from %s: %s", peer_id, exc)
            await websocket.close(code=1008, reason="Некорректное сообщение")
        except ConnectionClosed:
            pass

    async def send(self, message_type: str, payload: dict | None = None, recipient_id: str | None = None) -> None:
        raw = encode_message(Message(message_type, payload or {}, session_id=self.session_id))
        if self.is_host:
            if recipient_id is not None:
                websocket = self.clients.get(recipient_id)
                if websocket is None:
                    raise ConnectionError("Получатель отключился")
                await websocket.send(raw)
            else:
                results = await asyncio.gather(*(ws.send(raw) for ws in tuple(self.clients.values())),
                                               return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logging.warning("Broadcast failed: %s", result)
        elif self.websocket is not None:
            await self.websocket.send(raw)
        else:
            raise ConnectionError("Нет соединения")

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self) -> None:
        self._closing = True
        self._generation += 1
        connecting = self._connecting
        if connecting is not None and connecting is not asyncio.current_task():
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
        self._connecting = None
        sockets = tuple(self.clients.values()) + ((self.websocket,) if self.websocket else ())
        if self.server is not None:
            self.server.close()
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)
        self.clients.clear()
        self.websocket = None
        if self.server is not None:
            await self.server.wait_closed()
            self.server = None
        self._closing = False
