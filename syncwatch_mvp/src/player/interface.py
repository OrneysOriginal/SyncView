from __future__ import annotations

from abc import ABC, abstractmethod


class VideoPlayer(ABC):
    @abstractmethod
    def open(self, path: str) -> None: ...

    @abstractmethod
    def play(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def seek(self, position_ms: int) -> None: ...

    @abstractmethod
    def get_position_ms(self) -> int: ...

    @abstractmethod
    def get_duration_ms(self) -> int: ...

    @abstractmethod
    def is_playing(self) -> bool: ...
