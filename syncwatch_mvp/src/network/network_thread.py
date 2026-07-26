from __future__ import annotations

import asyncio
from typing import Any

from PySide6.QtCore import QThread, Signal

from src.network.connection_manager import ConnectionManager
from src.network.messages import Message


class NetworkThread(QThread):
    message_received = Signal(object)
    status_changed = Signal(str)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.manager: ConnectionManager | None = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def on_message(message: Message) -> None:
            self.message_received.emit(message)

        self.manager = ConnectionManager(on_message, self.status_changed.emit)
        self.loop.run_forever()
        self.loop.run_until_complete(self.manager.close())
        self.loop.close()

    def _submit(self, coro: Any) -> None:
        if self.loop is None:
            self.failed.emit("Сетевой поток ещё не запущен")
            return
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        future.add_done_callback(lambda f: self.failed.emit(str(f.exception())) if f.exception() else None)

    def host(self, port: int) -> None:
        assert self.manager is not None
        self._submit(self.manager.host(port))

    def connect_to(self, ip: str, port: int, room_code: str, device_name: str) -> None:
        assert self.manager is not None
        self._submit(self.manager.connect(ip, port, room_code, device_name))

    def send(self, message_type: str, payload: dict | None = None) -> None:
        assert self.manager is not None
        self._submit(self.manager.send(message_type, payload))

    def send_wait(
        self,
        message_type: str,
        payload: dict | None = None,
        timeout: float = 60.0,
    ) -> None:
        """Синхронная отправка для потоков передачи файла."""
        if self.loop is None or self.manager is None:
            raise RuntimeError("Сетевой поток ещё не запущен")
        future = asyncio.run_coroutine_threadsafe(
            self.manager.send(message_type, payload), self.loop
        )
        future.result(timeout=timeout)

    def shutdown(self) -> None:
        if self.loop is not None and self.manager is not None:
            future = asyncio.run_coroutine_threadsafe(self.manager.close(), self.loop)
            try:
                future.result(timeout=2)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.wait(2500)
