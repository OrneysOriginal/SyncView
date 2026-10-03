from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
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
        self._ready = threading.Event()
        self._receiver = None
        self._receiver_peer = ""
        self._disk = ThreadPoolExecutor(max_workers=1, thread_name_prefix="receive-file")

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def on_message(message: Message) -> None:
            receiver = self._receiver
            if (receiver is not None and message.type in {"file_chunk", "file_complete"}
                    and message.sender_id == self._receiver_peer
                    and message.payload.get("transfer_id") == receiver.transfer_id):
                try:
                    if message.type == "file_chunk":
                        fraction = await self.loop.run_in_executor(
                            self._disk, receiver.write_chunk, message.payload["index"], message.payload["data"])
                        event = Message("file_progress", {"transfer_id": receiver.transfer_id, "fraction": fraction},
                                        session_id=message.session_id, sender_id=message.sender_id)
                    else:
                        path = await self.loop.run_in_executor(self._disk, receiver.finalize,
                                                              message.payload["fingerprint"])
                        self._receiver = None
                        event = Message("file_saved", {"transfer_id": receiver.transfer_id, "path": str(path)},
                                        session_id=message.session_id, sender_id=message.sender_id)
                    self.message_received.emit(event)
                except Exception as exc:
                    logging.exception("Receiving file failed")
                    await self.loop.run_in_executor(self._disk, receiver.cleanup)
                    self._receiver = None
                    await self.manager.send("file_error", {"transfer_id": receiver.transfer_id, "reason": str(exc)},
                                            message.sender_id if self.manager.is_host else None)
                    self.message_received.emit(Message("file_error", {"transfer_id": receiver.transfer_id, "reason": str(exc)},
                                                       session_id=message.session_id, sender_id=message.sender_id))
                return
            self.message_received.emit(message)

        self.manager = ConnectionManager(on_message, self.status_changed.emit)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.manager.close())
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self._disk.shutdown(wait=True, cancel_futures=True)
            self.loop.close()
            self._ready.clear()

    def _submit(self, coro: Any):
        if not self._ready.wait(1) or self.loop is None or self.loop.is_closed():
            coro.close()
            self.failed.emit("Сетевой поток недоступен")
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def done(result):
            if result.cancelled():
                return
            error = result.exception()
            if error is not None:
                logging.error("Network operation failed: %s", error)
                self.failed.emit(str(error))
        future.add_done_callback(done)
        return future

    def host(self, port: int) -> None:
        if self._ready.wait(1):
            self._submit(self.manager.host(port))

    def connect_to(self, ip: str, port: int, room_code: str, device_name: str) -> None:
        if self._ready.wait(1):
            self._submit(self.manager.connect(ip, port, room_code, device_name))

    def send(self, message_type: str, payload: dict | None = None, recipient_id: str | None = None) -> None:
        if self._ready.wait(1):
            self._submit(self.manager.send(message_type, payload, recipient_id))

    def send_wait(self, message_type: str, payload: dict | None = None, timeout: float = 30.0,
                  recipient_id: str | None = None) -> None:
        if not self._ready.wait(1):
            raise RuntimeError("Сетевой поток недоступен")
        future = asyncio.run_coroutine_threadsafe(self.manager.send(message_type, payload, recipient_id), self.loop)
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def start_receiving(self, receiver, peer_id: str) -> None:
        def set_receiver():
            self._receiver = receiver
            self._receiver_peer = peer_id
        self.loop.call_soon_threadsafe(set_receiver)

    def stop_receiving(self) -> None:
        if not self._ready.wait(1) or self.loop is None or self.loop.is_closed():
            return
        def clear():
            receiver, self._receiver = self._receiver, None
            if receiver is not None:
                receiver.cancel_event.set()
                self._disk.submit(receiver.cleanup)
        self.loop.call_soon_threadsafe(clear)

    def leave_room(self) -> None:
        if not self._ready.wait(1):
            return
        self.stop_receiving()
        self._submit(self.manager.close())

    def shutdown(self) -> None:
        if self._ready.wait(1) and self.loop is not None:
            self.stop_receiving()
            self.loop.call_soon_threadsafe(self.loop.stop)
        # Wait for socket handlers and pending disk operations before Qt deletes
        # the thread. Native VLC runs outside this thread.
        self.wait()
