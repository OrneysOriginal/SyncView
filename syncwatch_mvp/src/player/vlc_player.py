from __future__ import annotations

import os
import sys
from pathlib import Path

import vlc
from PySide6.QtWidgets import QWidget

from src.player.interface import VideoPlayer


def create_vlc_instance(*options):
    instance = vlc.Instance("--no-video-title-show", *options)
    if instance is None:
        detail = vlc.libvlc_errmsg()
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(
            "VLC не смог инициализироваться. Проверьте полный комплект "
            "libvlc.dll, libvlccore.dll и plugins одной версии и архитектуры.\n"
            f"Библиотека: {os.environ.get('PYTHON_VLC_LIB_PATH', 'системный поиск')}\n"
            f"Плагины: {os.environ.get('VLC_PLUGIN_PATH', 'системный поиск')}\n"
            f"Сообщение VLC: {detail or 'подробности не предоставлены'}"
        )
    return instance


class VlcPlayer(VideoPlayer):
    def __init__(self, video_widget: QWidget, *, instance=None) -> None:
        owns_instance = instance is None
        self._instance = create_vlc_instance() if owns_instance else instance
        self._player = self._instance.media_player_new()
        if self._player is None:
            if owns_instance:
                self._instance.release()
            raise RuntimeError("VLC инициализирован, но не смог создать медиаплеер.")
        self._widget = video_widget
        # VLC по умолчанию перехватывает мышь внутри нативного видеовывода.
        # Отключаем это, чтобы Qt получал одиночные и двойные клики по видео.
        self._player.video_set_mouse_input(False)
        self._player.video_set_key_input(False)
        self.bind_video_output()

    def bind_video_output(self) -> None:
        self._player.video_set_mouse_input(False)
        self._player.video_set_key_input(False)
        wid = int(self._widget.winId())
        if sys.platform.startswith("linux"):
            self._player.set_xwindow(wid)
        elif sys.platform == "win32":
            self._player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self._player.set_nsobject(wid)

    def open(self, path: str) -> None:
        self.stop()
        media = self._instance.media_new(Path(path).resolve().as_uri())
        media.add_option(":start-paused")
        self._player.set_media(media)
        media.release()
        if self._player.play() == -1:
            raise RuntimeError("VLC не смог открыть файл")

    def is_ready(self) -> bool:
        return self._player.get_state() in {vlc.State.Playing, vlc.State.Paused} and self.get_duration_ms() > 0

    def has_error(self) -> bool:
        return self._player.get_state() == vlc.State.Error

    def video_frames_decoded(self) -> int:
        media = self._player.get_media()
        if media is None:
            return 0
        try:
            stats = vlc.MediaStats()
            return stats.decoded_video if media.get_stats(stats) else 0
        finally:
            media.release()

    def play(self) -> None:
        if self._player.play() == -1:
            raise RuntimeError("Ошибка воспроизведения VLC")

    def pause(self) -> None:
        self._player.set_pause(1)

    def stop(self) -> None:
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        position_ms = max(0, int(position_ms))
        duration = self.get_duration_ms()
        if duration:
            position_ms = min(position_ms, max(0, duration - 1))
        if self._player.set_time(position_ms) == -1:
            raise RuntimeError("VLC не смог изменить позицию видео")

    def release(self) -> None:
        self.stop()
        self._player.release()
        self._instance.release()

    def get_position_ms(self) -> int:
        return max(0, int(self._player.get_time()))

    def get_duration_ms(self) -> int:
        return max(0, int(self._player.get_length()))

    def is_playing(self) -> bool:
        return bool(self._player.is_playing())

    def set_volume(self, value: int) -> None:
        self._player.audio_set_volume(max(0, min(100, value)))
