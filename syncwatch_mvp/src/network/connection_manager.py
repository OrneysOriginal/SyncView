from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection

from src.network.messages import Message, PROTOCOL_VERSION
from src.network.protocol import MAX_MESSAGE_SIZE, decode_message, encode_message

MessageHandler = Callable[[Message], Awaitable[None]]
StatusHandler = Callable[[str], None]


class ConnectionManager:
    def __init__(self, on_message: MessageHandler, on_status: StatusHandler) -> None:
        self.on_message = on_message
        self.on_status = on_status
        self.session_id = secrets.token_hex(8)
        self.room_code = f"{secrets.randbelow(10000):04d}"
        self.websocket: ServerConnection | ClientConnection | None = None
        self.server = None
        self.is_host = False
        self._room_code_expected = ""

    async def host(self, port: int) -> None:
        self.is_host = True
        self._room_code_expected = self.room_code
        self.server = await websockets.serve(
            self._accept_client, "0.0.0.0", port, max_size=MAX_MESSAGE_SIZE
        )
        self.on_status("Сервер запущен. Ожидание подключения…")

    async def _accept_client(self, websocket: ServerConnection) -> None:
        if self.websocket is not None:
            await websocket.send(encode_message(Message("join_rejected", {"reason": "Второй клиент уже подключен"})))
            await websocket.close()
            return
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            join = decode_message(raw)
            if join.type != "join":
                raise ValueError("Ожидался запрос join")
            if str(join.payload.get("room_code")) != self._room_code_expected:
                await websocket.send(encode_message(Message("join_rejected", {"reason": "Неверный код"})))
                await websocket.close()
                return
            self.websocket = websocket
            await self.send("join_accepted", {"session_id": self.session_id})
            self.on_status("Клиент подключен")
            await self._listen(websocket)
        except Exception as exc:
            logging.exception("Client handler failed")
            self.on_status(f"Ошибка соединения: {exc}")
        finally:
            if self.websocket is websocket:
                self.websocket = None
                self.on_status("Соединение потеряно")

    async def connect(self, ip: str, port: int, room_code: str, device_name: str) -> None:
        self.is_host = False
        uri = f"ws://{ip}:{port}"
        self.on_status("Подключение…")
        websocket = await asyncio.wait_for(
            websockets.connect(uri, max_size=MAX_MESSAGE_SIZE), timeout=10
        )
        self.websocket = websocket
        await self.send("join", {
            "room_code": room_code,
            "device_name": device_name,
            "app_version": "0.1.0",
            "protocol_version": PROTOCOL_VERSION,
        })
        first = decode_message(await asyncio.wait_for(websocket.recv(), timeout=10))
        if first.type == "join_rejected":
            reason = str(first.payload.get("reason", "Соединение отклонено"))
            await websocket.close()
            self.websocket = None
            raise ConnectionError(reason)
        if first.type != "join_accepted":
            raise ConnectionError("Некорректный ответ сервера")
        self.session_id = str(first.payload.get("session_id", first.session_id))
        self.on_status("Подключено")
        await self.on_message(first)
        await self._listen(websocket)

    async def _listen(self, websocket: ServerConnection | ClientConnection) -> None:
        try:
            async for raw in websocket:
                message = decode_message(raw)
                if message.type == "ping":
                    await self.send("pong", {"echo": message.sent_at, "remote_time": time.monotonic()})
                else:
                    await self.on_message(message)
        finally:
            if self.websocket is websocket:
                self.websocket = None

    async def send(self, message_type: str, payload: dict | None = None) -> None:
        if self.websocket is None:
            return
        message = Message(message_type, payload or {}, session_id=self.session_id)
        await self.websocket.send(encode_message(message))

    async def close(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
