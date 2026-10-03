from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, Signal, QObject
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from src.media.fingerprint import calculate_fingerprint
from src.media.metadata import MediaInfo, basic_file_data
from src.media.validator import media_matches
from src.network.file_transfer import FileTransferReceiver, FileTransferSender
from src.network.messages import Message
from src.network.network_thread import NetworkThread
from src.player.vlc_player import VlcPlayer
from src.session.role import Role
from src.session.state import SessionState
from src.synchronization.drift_calculator import calculate_drift_ms, needs_correction
from src.ui.theme import APP_STYLE


class VideoSurface(QWidget):
    clicked = Signal()
    double_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setMouseTracking(True)
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.setInterval(220)
        self._single_click_timer.timeout.connect(self.clicked.emit)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._single_click_timer.start()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._single_click_timer.stop()
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class TransferSignals(QObject):
    progress = Signal(float, str)
    finished = Signal()
    failed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, *, vlc_instance=None) -> None:
        super().__init__()
        self.setWindowTitle("SyncWatch")
        self.resize(1080, 720)
        self.setMinimumSize(860, 600)
        self.setStyleSheet(APP_STYLE)
        self.state = SessionState()
        self.network = NetworkThread()
        self.network.message_received.connect(self._on_message)
        self.network.status_changed.connect(self._set_status)
        self.network.failed.connect(self._show_error)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.command_id = 0
        self.last_command_id = 0
        self.local_media: MediaInfo | None = None
        self.remote_media: MediaInfo | None = None
        self.local_media_path: Path | None = None
        self.dragging = False
        self.last_correction = 0.0
        self._fullscreen = False
        self._last_volume = 80
        self._controls_hidden = False
        self.transfer_active = False
        self._transfer_sender: FileTransferSender | None = None
        self._transfer_receiver: FileTransferReceiver | None = None
        self._transfer_signals: TransferSignals | None = None
        self._incoming_offer_id: str | None = None

        self.controls_hide_timer = QTimer(self)
        self.controls_hide_timer.setSingleShot(True)
        self.controls_hide_timer.setInterval(2500)
        self.controls_hide_timer.timeout.connect(self._hide_player_controls)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self._build_home()
        self._build_host()
        self._build_connect()
        self._build_player(vlc_instance)
        # Start background work only after VLC has initialized successfully.
        self.network.start()

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_player_ui)
        self.ui_timer.start(250)
        self.state_timer = QTimer(self)
        self.state_timer.timeout.connect(self._send_state)
        self.state_timer.start(500)

    def _shell(self, active: str, content: QWidget) -> QWidget:
        root = QWidget()
        root.setObjectName("appRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(58)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(22, 0, 22, 0)
        brand = QLabel("◉  SyncWatch")
        brand.setObjectName("brand")
        top.addWidget(brand)
        top.addStretch()
        section = {
            "home": "Главная",
            "room": "Комната",
            "player": "Просмотр",
        }.get(active, "")
        section_label = QLabel(section)
        section_label.setObjectName("muted")
        top.addWidget(section_label)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(34, 30, 34, 30)
        body_layout.addWidget(content)
        outer.addWidget(topbar)
        outer.addWidget(body, 1)
        if active == "player":
            self.player_shell_root = root
            self.player_shell_topbar = topbar
            self.player_shell_body = body
            self.player_shell_body_layout = body_layout
        return root

    @staticmethod
    @staticmethod
    def _card() -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        return frame, layout

    def _build_home(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        layout.addStretch()
        logo = QLabel("▶")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("font-size:42px; color:#5B5FEF;")
        title = QLabel("SyncWatch")
        title.setObjectName("heroTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("Синхронный просмотр локального видео на двух устройствах")
        subtitle.setObjectName("muted")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(18)

        actions = QVBoxLayout()
        actions.setSpacing(10)
        actions.setContentsMargins(0, 0, 0, 0)
        create = QPushButton("Создать комнату")
        create.setObjectName("primary")
        create.setFixedWidth(260)
        create.clicked.connect(self._create_room)
        connect = QPushButton("Подключиться")
        connect.setFixedWidth(260)
        connect.clicked.connect(lambda: self.stack.setCurrentWidget(self.connect_page))
        actions.addWidget(create, alignment=Qt.AlignmentFlag.AlignHCenter)
        actions.addWidget(connect, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(actions)
        layout.addStretch()

        footer = QLabel("Работает в локальной сети • видео не передаётся между устройствами")
        footer.setObjectName("muted")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(footer)

        self.home_page = self._shell("home", content)
        self.stack.addWidget(self.home_page)

    def _build_host(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        title = QLabel("Комната создана")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Передайте IP, порт и код второму участнику.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card, card_l = self._card()
        self.host_info = QLabel()
        self.host_info.setObjectName("metricValue")
        self.host_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.host_info.setWordWrap(True)
        self.host_status = QLabel("Подготовка…")
        self.host_status.setObjectName("muted")
        card_l.addWidget(self.host_info)
        card_l.addSpacing(8)
        card_l.addWidget(self.host_status)
        layout.addWidget(card)

        actions = QHBoxLayout()
        choose = QPushButton("Выбрать видео")
        choose.setObjectName("primary")
        choose.clicked.connect(self._choose_media)
        close = QPushButton("Закрыть комнату")
        close.setObjectName("danger")
        close.clicked.connect(self._go_home)
        actions.addWidget(choose)
        actions.addWidget(close)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        self.host_page = self._shell("room", content)
        self.stack.addWidget(self.host_page)

    def _build_connect(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        title = QLabel("Подключиться к комнате")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Введите данные, показанные на ведущем устройстве.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card, card_l = self._card()
        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(24)
        self.ip_edit = QLineEdit("127.0.0.1")
        self.ip_edit.setPlaceholderText("192.168.1.34")
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(45892)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("Например, 7315")
        self.name_edit = QLineEdit(socket.gethostname())
        form.addRow("IP-адрес", self.ip_edit)
        form.addRow("Порт", self.port_edit)
        form.addRow("Код комнаты", self.code_edit)
        form.addRow("Имя устройства", self.name_edit)
        card_l.addLayout(form)
        self.connect_status = QLabel("Готово к подключению")
        self.connect_status.setObjectName("muted")
        card_l.addWidget(self.connect_status)
        layout.addWidget(card)

        actions = QHBoxLayout()
        connect = QPushButton("Подключиться")
        connect.setObjectName("primary")
        connect.clicked.connect(self._connect_room)
        cancel = QPushButton("Назад")
        cancel.clicked.connect(lambda: self.stack.setCurrentWidget(self.home_page))
        actions.addWidget(connect)
        actions.addWidget(cancel)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        self.connect_page = self._shell("room", content)
        self.stack.addWidget(self.connect_page)

    def _build_player(self, vlc_instance=None) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.player_header = QWidget()
        header = QHBoxLayout(self.player_header)
        header.setContentsMargins(0, 0, 0, 0)
        self.player_title = QLabel("Совместный просмотр")
        self.player_title.setObjectName("sectionTitle")
        self.connection_badge = QLabel("● Подключение…")
        self.connection_badge.setObjectName("muted")
        header.addWidget(self.player_title)
        header.addStretch()
        header.addWidget(self.connection_badge)
        layout.addWidget(self.player_header)

        self.video_card = QFrame()
        self.video_card.setObjectName("videoCard")
        video_layout = QVBoxLayout(self.video_card)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)

        self.video_surface = VideoSurface()
        self.video_surface.setMinimumHeight(430)
        self.video_surface.setStyleSheet(
            "background:#0D0F13; border-top-left-radius:12px; border-top-right-radius:12px;"
        )
        self.video_surface.clicked.connect(self._toggle_playback)
        self.video_surface.double_clicked.connect(self._toggle_fullscreen)
        self.player = VlcPlayer(self.video_surface, instance=vlc_instance)
        video_layout.addWidget(self.video_surface, 1)

        self.player_controls = QFrame()
        self.player_controls.setObjectName("playerControls")
        controls = QHBoxLayout(self.player_controls)
        controls.setContentsMargins(14, 10, 14, 10)
        controls.setSpacing(10)

        self.play_pause_button = QPushButton("▶")
        self.play_pause_button.setObjectName("playerIconButton")
        self.play_pause_button.setFixedSize(40, 36)
        self.play_pause_button.setToolTip("Воспроизвести / поставить на паузу")
        self.play_pause_button.clicked.connect(self._toggle_playback)

        self.time_label = QLabel("00:00")
        self.time_label.setObjectName("playerTime")
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 1000)
        self.timeline.sliderPressed.connect(self._timeline_pressed)
        self.timeline.sliderReleased.connect(self._timeline_released)
        self.duration_label = QLabel("00:00")
        self.duration_label.setObjectName("playerTime")

        self.mute_button = QPushButton("🔊")
        self.mute_button.setObjectName("playerIconButton")
        self.mute_button.setFixedSize(40, 36)
        self.mute_button.setToolTip("Выключить / включить звук")
        self.mute_button.clicked.connect(self._toggle_mute)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(120)
        self.volume.valueChanged.connect(self._set_volume)

        self.fullscreen_button = QPushButton("⛶")
        self.fullscreen_button.setObjectName("playerIconButton")
        self.fullscreen_button.setFixedSize(40, 36)
        self.fullscreen_button.setToolTip("Полноэкранный режим")
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)

        controls.addWidget(self.play_pause_button)
        controls.addWidget(self.time_label)
        controls.addWidget(self.timeline, 1)
        controls.addWidget(self.duration_label)
        controls.addSpacing(6)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.volume)
        controls.addWidget(self.fullscreen_button)
        video_layout.addWidget(self.player_controls)
        layout.addWidget(self.video_card, 1)

        # Отслеживаем активность мыши над всей областью проигрывателя.
        # Панель управления появляется сразу и скрывается после бездействия.
        self._install_player_activity_filter(self.video_card)
        self._show_player_controls(restart_timer=False)

        self.info_card = QFrame()
        self.info_card.setObjectName("softCard")
        info_layout = QVBoxLayout(self.info_card)
        info_layout.setContentsMargins(14, 10, 14, 10)
        info_layout.setSpacing(8)

        info = QHBoxLayout()
        self.player_status = QLabel("Файл не выбран")
        self.player_status.setWordWrap(True)
        self.player_status.setObjectName("muted")
        self.role_label = QLabel("Роль: —")
        self.role_label.setObjectName("muted")
        self.ping_label = QLabel("Ping: —")
        self.ping_label.setObjectName("muted")
        self.drift_label = QLabel("Рассинхрон: —")
        self.drift_label.setObjectName("muted")
        choose = QPushButton("Сменить файл")
        choose.clicked.connect(self._choose_media)
        self.send_file_button = QPushButton("Отправить файл")
        self.send_file_button.setToolTip("Отправить выбранный видеофайл на другое устройство по локальной сети")
        self.send_file_button.clicked.connect(self._start_file_offer)
        self.send_file_button.hide()
        self.cancel_transfer_button = QPushButton("Отменить передачу")
        self.cancel_transfer_button.clicked.connect(self._cancel_file_transfer)
        self.cancel_transfer_button.hide()
        self.force_match_button = QPushButton("Продолжить без совпадения")
        self.force_match_button.setToolTip(
            "Разрешить синхронизацию, даже если размер или отпечаток файлов различаются. "
            "Полезно, если одно и то же видео скачано через мессенджер или другой источник."
        )
        self.force_match_button.clicked.connect(self._force_media_match)
        self.force_match_button.hide()
        info.addWidget(self.player_status, 1)
        info.addWidget(self.role_label)
        info.addWidget(self.ping_label)
        info.addWidget(self.drift_label)
        info.addWidget(self.force_match_button)
        info.addWidget(self.send_file_button)
        info.addWidget(self.cancel_transfer_button)
        info.addWidget(choose)
        info_layout.addLayout(info)

        self.transfer_progress = QProgressBar()
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_progress.setTextVisible(True)
        self.transfer_progress.hide()
        info_layout.addWidget(self.transfer_progress)
        layout.addWidget(self.info_card)

        self.player_page = self._shell("player", content)
        self.stack.addWidget(self.player_page)

    def _install_player_activity_filter(self, widget: QWidget) -> None:
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if hasattr(self, "video_card") and (watched is self.video_card or self.video_card.isAncestorOf(watched)):
            if event.type() in {
                QEvent.Type.MouseMove,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.Wheel,
                QEvent.Type.Enter,
            }:
                self._show_player_controls()
        return super().eventFilter(watched, event)

    def _show_player_controls(self, restart_timer: bool = True) -> None:
        if not hasattr(self, "player_controls"):
            return
        if self._controls_hidden:
            self.player_controls.show()
            self._controls_hidden = False
        self.video_surface.setCursor(Qt.CursorShape.ArrowCursor)
        if restart_timer:
            self.controls_hide_timer.stop()
            if self.player.is_playing() and self.stack.currentWidget() == self.player_page:
                self.controls_hide_timer.start()

    def _hide_player_controls(self) -> None:
        if (
            self.stack.currentWidget() != self.player_page
            or not self.player.is_playing()
            or self.dragging
        ):
            return
        self.player_controls.hide()
        self._controls_hidden = True
        if self._fullscreen:
            self.video_surface.setCursor(Qt.CursorShape.BlankCursor)

    def _create_room(self) -> None:
        self.state.role = Role.HOST
        ip = self._local_ip()
        port = 45892
        code = self.network.manager.room_code if self.network.manager else "----"
        self.host_info.setText(f"IP:  {ip}    •    Порт:  {port}    •    Код:  {code}")
        self.stack.setCurrentWidget(self.host_page)
        self.network.host(port)

    def _connect_room(self) -> None:
        self.state.role = Role.CLIENT
        self.network.connect_to(
            self.ip_edit.text().strip(), self.port_edit.value(), self.code_edit.text().strip(), self.name_edit.text().strip()
        )

    def _choose_media(self) -> None:
        if self.transfer_active:
            self._show_error("Дождитесь окончания передачи файла")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Выберите видео", "", "Видео (*.mp4 *.mkv *.mov *.avi *.m4v)")
        if not path:
            return
        self._start_media_load(path)

    def _start_media_load(self, path: str | Path) -> None:
        media_path = Path(path)
        self._reset_media_match_state()
        self.local_media_path = media_path
        self.player_status.setText("Открытие файла и расчёт отпечатка…")
        self.player.open(str(media_path))
        signals = WorkerSignals()
        signals.finished.connect(self._media_ready)
        signals.failed.connect(self._show_error)

        def work() -> MediaInfo:
            name, size = basic_file_data(media_path)
            fingerprint = calculate_fingerprint(media_path)
            duration = 0
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and duration <= 0:
                duration = self.player.get_duration_ms()
                time.sleep(0.1)
            return MediaInfo(name, size, duration, fingerprint)

        future = self.executor.submit(work)
        future.add_done_callback(
            lambda f: signals.failed.emit(str(f.exception())) if f.exception() else signals.finished.emit(f.result())
        )
        self._worker_signals = signals

    def _media_ready(self, media: MediaInfo) -> None:
        self.local_media = media
        self.state.local_ready = True
        self.stack.setCurrentWidget(self.player_page)
        self.player_status.setText(f"Файл выбран: {media.file_name}. Ожидание второго устройства…")
        self.network.send("media_info", media.to_dict())
        self.network.send("ready", {"media_loaded": True})
        self._check_media_match()
        self._update_transfer_controls()

    def _reset_media_match_state(self) -> None:
        self.state.media_match = False
        self.state.media_force_match = False
        if hasattr(self, "force_match_button"):
            self.force_match_button.hide()

    def _check_media_match(self) -> None:
        if not self.local_media or not self.remote_media:
            return
        exact = media_matches(self.local_media, self.remote_media)
        if exact:
            self.state.media_force_match = False
            self.state.media_match = True
            self.force_match_button.hide()
            self.player_status.setText("Файлы совпадают. Оба устройства готовы.")
            self.network.send("media_match")
            self._update_transfer_controls()
            return

        if self.state.media_force_match:
            self.state.media_match = True
            self.force_match_button.hide()
            self.player_status.setText(
                "Файлы не совпадают, но продолжение уже разрешено. Синхронизация может быть менее точной."
            )
            self._update_transfer_controls()
            return

        self.state.media_match = False
        self.force_match_button.show()
        self.player_status.setText(
            "Файлы не совпадают (размер или отпечаток). Можно отправить файл другому устройству "
            "или продолжить без точного совпадения."
        )
        self.network.send("media_mismatch")
        self._update_transfer_controls()

    def _force_media_match(self) -> None:
        if not self.local_media or not self.remote_media:
            return
        if media_matches(self.local_media, self.remote_media):
            self._check_media_match()
            return
        self._apply_media_force_match(send_to_peer=True)

    def _apply_media_force_match(self, *, send_to_peer: bool) -> None:
        self.state.media_force_match = True
        self.state.media_match = True
        self.force_match_button.hide()
        self.player_status.setText(
            "Продолжение без точного совпадения. Ведущий может запускать синхронизацию; "
            "при разной длительности возможен рассинхрон."
        )
        if send_to_peer:
            self.network.send("media_force_match")

    def _playback_allowed(self) -> bool:
        if self.transfer_active:
            return False
        return self.state.media_match or self.state.media_force_match

    def _is_connected(self) -> bool:
        return bool(self.network.manager and self.network.manager.websocket)

    def _update_transfer_controls(self) -> None:
        if not hasattr(self, "send_file_button"):
            return
        if self.transfer_active:
            self.send_file_button.hide()
            self.cancel_transfer_button.show()
            self.transfer_progress.show()
            return
        self.cancel_transfer_button.hide()
        self.transfer_progress.hide()
        self.transfer_progress.setValue(0)
        can_send = (
            self.local_media is not None
            and self.local_media_path is not None
            and self._is_connected()
            and not self.transfer_active
        )
        self.send_file_button.setVisible(can_send)

    def _set_transfer_progress(self, fraction: float, text: str) -> None:
        self.transfer_progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))
        self.player_status.setText(text)

    def _start_file_offer(self) -> None:
        if self.transfer_active or not self.local_media or not self.local_media_path:
            return
        if not self._is_connected():
            self._show_error("Нет соединения с другим устройством")
            return
        if not self.local_media_path.exists():
            self._show_error("Выбранный файл больше недоступен")
            return

        sender = FileTransferSender(
            self.local_media_path,
            self.local_media,
            send=self.network.send_wait,
        )
        self._transfer_sender = sender
        self.transfer_active = True
        self._update_transfer_controls()
        self.player_status.setText("Предложение отправить файл… Ожидание ответа.")
        self.network.send("file_offer", sender.offer_payload())

    def _begin_outgoing_transfer(self) -> None:
        sender = self._transfer_sender
        if sender is None:
            return
        signals = TransferSignals()
        signals.progress.connect(self._set_transfer_progress)
        signals.finished.connect(self._on_outgoing_transfer_finished)
        signals.failed.connect(self._on_transfer_failed)
        self._transfer_signals = signals

        def work() -> None:
            try:
                sender.on_progress = lambda fraction, text: signals.progress.emit(fraction, text)
                sender.run()
                if not sender.cancel_event.is_set():
                    signals.finished.emit()
            except Exception as exc:
                signals.failed.emit(str(exc))

        self.executor.submit(work)

    def _on_outgoing_transfer_finished(self) -> None:
        self.transfer_active = False
        self._transfer_sender = None
        self._update_transfer_controls()
        self.player_status.setText("Файл отправлен. Ожидание, пока второе устройство откроет копию…")

    def _on_transfer_failed(self, reason: str) -> None:
        self._cleanup_transfer_state(send_cancel=False)
        text = reason if reason and reason != "None" else "Ошибка передачи файла"
        self.player_status.setText(text)
        self._show_error(text)
        self._update_transfer_controls()

    def _cancel_file_transfer(self) -> None:
        if self._transfer_sender is not None:
            self._transfer_sender.cancel()
        if self._transfer_receiver is not None:
            transfer_id = self._transfer_receiver.transfer_id
            self._transfer_receiver.cancel()
            self.network.send("file_cancel", {"transfer_id": transfer_id})
        elif self._transfer_sender is not None:
            self.network.send("file_cancel", {"transfer_id": self._transfer_sender.transfer_id})
        self._cleanup_transfer_state(send_cancel=False)
        self.player_status.setText("Передача файла отменена.")
        self._update_transfer_controls()

    def _cleanup_transfer_state(self, *, send_cancel: bool) -> None:
        if send_cancel and self._transfer_sender is not None:
            self.network.send("file_cancel", {"transfer_id": self._transfer_sender.transfer_id})
        if send_cancel and self._transfer_receiver is not None:
            self.network.send("file_cancel", {"transfer_id": self._transfer_receiver.transfer_id})
            self._transfer_receiver.cancel()
        elif self._transfer_receiver is not None:
            self._transfer_receiver.cleanup()
        self.transfer_active = False
        self._transfer_sender = None
        self._transfer_receiver = None

    def _handle_file_offer(self, payload: dict) -> None:
        transfer_id = str(payload.get("transfer_id", ""))
        if self.transfer_active:
            self.network.send(
                "file_reject",
                {"transfer_id": transfer_id, "reason": "Уже идёт другая передача"},
            )
            return
        name = str(payload.get("file_name", "video"))
        size = int(payload.get("file_size", 0))
        size_mb = size / (1024 * 1024) if size else 0
        self._incoming_offer_id = transfer_id
        answer = QMessageBox.question(
            self,
            "Получение файла",
            f"Другое устройство предлагает отправить файл:\n\n{name}\nРазмер: {size_mb:.1f} МБ\n\n"
            f"Файл будет сохранён в ~/.syncwatch/received и открыт для совместного просмотра.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if self._incoming_offer_id != transfer_id:
            return
        self._incoming_offer_id = None
        if answer != QMessageBox.StandardButton.Yes:
            self.network.send("file_reject", {"transfer_id": transfer_id, "reason": "Отклонено пользователем"})
            return
        try:
            self._transfer_receiver = FileTransferReceiver(payload)
        except Exception as exc:
            self.network.send("file_error", {"transfer_id": transfer_id, "reason": str(exc)})
            self._show_error(str(exc))
            return
        self.transfer_active = True
        self._update_transfer_controls()
        self._set_transfer_progress(0.0, f"Получение «{name}»…")
        self.network.send("file_accept", {"transfer_id": transfer_id})

    def _handle_file_chunk(self, payload: dict) -> None:
        receiver = self._transfer_receiver
        if receiver is None:
            return
        if str(payload.get("transfer_id")) != receiver.transfer_id:
            return
        try:
            fraction = receiver.write_chunk(int(payload["index"]), str(payload["data"]))
            self._set_transfer_progress(fraction, f"Получение {int(fraction * 100)}%…")
        except Exception as exc:
            transfer_id = receiver.transfer_id
            receiver.cancel()
            self._cleanup_transfer_state(send_cancel=False)
            self.network.send("file_error", {"transfer_id": transfer_id, "reason": str(exc)})
            self._on_transfer_failed(str(exc))

    def _handle_file_complete(self, payload: dict) -> None:
        receiver = self._transfer_receiver
        if receiver is None:
            return
        if str(payload.get("transfer_id")) != receiver.transfer_id:
            return
        try:
            path = receiver.finalize(str(payload.get("fingerprint", "")))
        except Exception as exc:
            transfer_id = receiver.transfer_id
            self._cleanup_transfer_state(send_cancel=False)
            self.network.send("file_error", {"transfer_id": transfer_id, "reason": str(exc)})
            self._on_transfer_failed(str(exc))
            return
        self._cleanup_transfer_state(send_cancel=False)
        self._update_transfer_controls()
        self.player_status.setText(f"Файл получен: {path.name}. Открытие…")
        self._start_media_load(path)

    def _on_message(self, message: Message) -> None:
        p = message.payload
        if message.type == "join_accepted":
            self.state.connected = True
            self.stack.setCurrentWidget(self.player_page)
            self.player_status.setText("Подключено. Выберите видеофайл.")
            self._update_transfer_controls()
        elif message.type == "media_info":
            self.remote_media = MediaInfo.from_dict(p)
            self.state.media_force_match = False
            self._check_media_match()
        elif message.type == "ready":
            self.state.remote_ready = bool(p.get("media_loaded"))
        elif message.type == "media_force_match":
            if self.local_media and self.remote_media:
                self._apply_media_force_match(send_to_peer=False)
        elif message.type == "file_offer":
            self._handle_file_offer(p)
        elif message.type == "file_accept":
            if self._transfer_sender and str(p.get("transfer_id")) == self._transfer_sender.transfer_id:
                self.player_status.setText("Передача принята. Отправка файла…")
                self._begin_outgoing_transfer()
        elif message.type == "file_reject":
            if self._transfer_sender and str(p.get("transfer_id")) == self._transfer_sender.transfer_id:
                reason = str(p.get("reason", "Передача отклонена"))
                self._cleanup_transfer_state(send_cancel=False)
                self.player_status.setText(reason)
                self._update_transfer_controls()
        elif message.type == "file_chunk":
            self._handle_file_chunk(p)
        elif message.type == "file_complete":
            self._handle_file_complete(p)
        elif message.type == "file_cancel":
            cancel_id = str(p.get("transfer_id", ""))
            if self._incoming_offer_id and cancel_id == self._incoming_offer_id:
                self._incoming_offer_id = None
                self.player_status.setText("Предложение передачи файла отменено.")
            elif self._transfer_receiver and cancel_id == self._transfer_receiver.transfer_id:
                self._transfer_receiver.cancel()
                self._cleanup_transfer_state(send_cancel=False)
                self.player_status.setText("Передача файла отменена отправителем.")
                self._update_transfer_controls()
            elif self._transfer_sender and cancel_id == self._transfer_sender.transfer_id:
                self._transfer_sender.cancel()
                self._cleanup_transfer_state(send_cancel=False)
                self.player_status.setText("Передача файла отменена получателем.")
                self._update_transfer_controls()
        elif message.type == "file_error":
            if (
                (self._transfer_sender and str(p.get("transfer_id")) == self._transfer_sender.transfer_id)
                or (self._transfer_receiver and str(p.get("transfer_id")) == self._transfer_receiver.transfer_id)
            ):
                if self._transfer_sender is not None:
                    self._transfer_sender.cancel()
                if self._transfer_receiver is not None:
                    self._transfer_receiver.cancel()
                reason = str(p.get("reason", "Ошибка передачи файла"))
                self._cleanup_transfer_state(send_cancel=False)
                self.player_status.setText(reason)
                self._update_transfer_controls()
                self._show_error(reason)
        elif message.type in {"play", "pause", "seek", "sync_correction"}:
            self._schedule_remote_command(message)
        elif message.type == "state" and self.state.role == Role.HOST:
            self._handle_remote_state(message)
        elif message.type == "pong":
            sent = float(p.get("echo", time.monotonic()))
            self.state.ping_ms = max(0.0, (time.monotonic() - sent) * 1000)

    def _schedule_remote_command(self, message: Message) -> None:
        command_id = int(message.payload.get("command_id", 0))
        if command_id <= self.last_command_id:
            return
        self.last_command_id = command_id
        # Монотонные часы разных компьютеров имеют разные точки отсчёта.
        # Поэтому для сетевых команд используем относительную задержку,
        # отсчитываемую ведомым с момента получения сообщения.
        if "execute_delay_ms" in message.payload:
            delay_ms = max(0, min(5000, int(message.payload["execute_delay_ms"])))
        else:
            # Совместимость со старыми сообщениями. Не разрешаем случайно
            # запланировать команду на часы или дни вперёд.
            execute_at = float(message.payload.get("execute_at", time.monotonic()))
            delay_ms = max(0, min(2000, int((execute_at - time.monotonic()) * 1000)))
        QTimer.singleShot(delay_ms, lambda: self._execute_command(message))

    def _execute_command(self, message: Message) -> None:
        position = int(message.payload.get("position_ms", self.player.get_position_ms()))
        if message.type == "play":
            self.player.seek(position)
            self.player.play()
            self._show_player_controls()
        elif message.type == "pause":
            self.player.seek(position)
            self.player.pause()
            self.controls_hide_timer.stop()
            self._show_player_controls(restart_timer=False)
        elif message.type in {"seek", "sync_correction"}:
            self.player.seek(position)
            if bool(message.payload.get("resume", self.player.is_playing())):
                self.player.play()
            else:
                self.player.pause()

    def _next_command(self) -> int:
        self.command_id += 1
        return self.command_id

    def _toggle_playback(self) -> None:
        self._show_player_controls(restart_timer=False)
        if self.state.role != Role.HOST:
            return
        if self.player.is_playing():
            self._host_pause()
            return
        if not self._playback_allowed():
            self.player_status.setText(
                "Сначала дождитесь совпадения файлов или нажмите «Продолжить без совпадения»."
            )
            return
        self._host_play()

    def _set_volume(self, value: int) -> None:
        self.player.set_volume(value)
        if value > 0:
            self._last_volume = value
        self.mute_button.setText("🔇" if value == 0 else "🔊")

    def _toggle_mute(self) -> None:
        if self.volume.value() == 0:
            self.volume.setValue(max(1, self._last_volume))
        else:
            self._last_volume = self.volume.value()
            self.volume.setValue(0)

    def _toggle_fullscreen(self) -> None:
        if self._fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        if self._fullscreen:
            return
        self._fullscreen = True
        self.player_shell_topbar.hide()
        self.player_header.hide()
        self.info_card.hide()
        self.player_shell_body_layout.setContentsMargins(0, 0, 0, 0)
        # В полноэкранном режиме все контейнеры вокруг VLC должны быть чёрными.
        # Иначе системный фон светлой темы виден полосами при несовпадении пропорций.
        self.stack.setStyleSheet("background:#000;")
        self.player_page.setStyleSheet("background:#000;")
        self.player_shell_root.setStyleSheet("background:#000;")
        self.player_shell_body.setStyleSheet("background:#000;")
        self.video_card.setStyleSheet("background:#000; border:none; border-radius:0;")
        self.video_surface.setStyleSheet("background:#000; border:none; border-radius:0;")
        self.fullscreen_button.setText("🗗")
        self._show_player_controls()
        self.showFullScreen()
        QTimer.singleShot(0, self.player.bind_video_output)

    def _exit_fullscreen(self) -> None:
        if not self._fullscreen:
            return
        self._fullscreen = False
        self.showNormal()
        self.player_shell_topbar.show()
        self.player_header.show()
        self.info_card.show()
        self.player_shell_body_layout.setContentsMargins(34, 30, 34, 30)
        self.stack.setStyleSheet("")
        self.player_page.setStyleSheet("")
        self.player_shell_root.setStyleSheet("")
        self.player_shell_body.setStyleSheet("")
        self.video_card.setStyleSheet("")
        self.video_surface.setStyleSheet(
            "background:#0D0F13; border-top-left-radius:12px; border-top-right-radius:12px;"
        )
        self.fullscreen_button.setText("⛶")
        self._show_player_controls()
        QTimer.singleShot(0, self.player.bind_video_output)

    def _host_play(self) -> None:
        if self.state.role != Role.HOST or not self._playback_allowed():
            return
        delay_ms = 350
        position = self.player.get_position_ms()
        command = {
            "command_id": self._next_command(),
            "position_ms": position,
            "execute_delay_ms": delay_ms,
        }
        self.network.send("play", command)
        QTimer.singleShot(delay_ms, self.player.play)
        QTimer.singleShot(delay_ms + 50, self._show_player_controls)

    def _host_pause(self) -> None:
        if self.state.role != Role.HOST:
            return
        delay_ms = 350
        position = self.player.get_position_ms()
        command = {
            "command_id": self._next_command(),
            "position_ms": position,
            "execute_delay_ms": delay_ms,
        }
        self.network.send("pause", command)
        QTimer.singleShot(delay_ms, self.player.pause)
        self.controls_hide_timer.stop()
        self._show_player_controls(restart_timer=False)

    def _seek_relative(self, delta_ms: int) -> None:
        """Перемотать видео относительно текущей позиции и синхронизировать ведомое устройство."""
        if (
            self.state.role != Role.HOST
            or self.stack.currentWidget() != self.player_page
            or not self._playback_allowed()
        ):
            return

        duration = self.player.get_duration_ms()
        current = self.player.get_position_ms()
        if duration <= 0:
            return

        target = max(0, min(duration, current + delta_ms))
        resume = self.player.is_playing()
        delay_ms = 350
        command = {
            "command_id": self._next_command(),
            "position_ms": target,
            "resume": resume,
            "execute_delay_ms": delay_ms,
        }
        self.network.send("seek", command)
        QTimer.singleShot(delay_ms, lambda: self._execute_command(Message("seek", command)))
        self._show_player_controls()

    def _timeline_pressed(self) -> None:
        self.dragging = True
        self.controls_hide_timer.stop()
        self._show_player_controls(restart_timer=False)

    def _timeline_released(self) -> None:
        self.dragging = False
        self._show_player_controls()
        if self.state.role != Role.HOST or not self._playback_allowed():
            return
        duration = self.player.get_duration_ms()
        target = int(duration * self.timeline.value() / 1000) if duration else 0
        resume = self.player.is_playing()
        self.player.pause()
        delay_ms = 350
        command = {
            "command_id": self._next_command(),
            "position_ms": target,
            "resume": resume,
            "execute_delay_ms": delay_ms,
        }
        self.network.send("seek", command)
        QTimer.singleShot(delay_ms, lambda: self._execute_command(Message("seek", command)))

    def _send_state(self) -> None:
        if self.state.role == Role.CLIENT and self.network.manager and self.network.manager.websocket:
            self.network.send("state", {
                "position_ms": self.player.get_position_ms(),
                "playing": self.player.is_playing(),
                "rate": 1.0,
                "last_command_id": self.last_command_id,
                "local_time": time.monotonic(),
            })
        elif self.network.manager and self.network.manager.websocket:
            self.network.send("ping")

    def _handle_remote_state(self, message: Message) -> None:
        expected = self.player.get_position_ms()
        remote = int(message.payload.get("position_ms", expected))
        drift = calculate_drift_ms(expected, remote)
        self.state.drift_ms = drift
        now = time.monotonic()
        if needs_correction(drift) and now - self.last_correction >= 3:
            self.last_correction = now
            command = {
                "command_id": self._next_command(),
                "position_ms": expected,
                "resume": self.player.is_playing(),
                "execute_delay_ms": 350,
            }
            self.network.send("sync_correction", command)

    def _refresh_player_ui(self) -> None:
        position = self.player.get_position_ms()
        duration = self.player.get_duration_ms()
        if duration > 0 and not self.dragging:
            self.timeline.setValue(int(position * 1000 / duration))
        self.time_label.setText(self._fmt(position))
        self.duration_label.setText(self._fmt(duration))
        self.play_pause_button.setText("Ⅱ" if self.player.is_playing() else "▶")
        host_controls = self.state.role == Role.HOST and (
            self.player.is_playing() or self._playback_allowed()
        )
        self.play_pause_button.setEnabled(host_controls)
        self.timeline.setEnabled(self.state.role == Role.HOST and self._playback_allowed())
        role = "Ведущий" if self.state.role == Role.HOST else "Ведомый"
        self.role_label.setText(f"Роль: {role}")
        self.ping_label.setText(f"Ping: {self.state.ping_ms:.0f} мс")
        self.drift_label.setText(f"Рассинхрон: {self.state.drift_ms} мс")
        self.player_title.setText("Управление просмотром" if self.state.role == Role.HOST else "Совместный просмотр")
        self._update_transfer_controls()

    def _set_status(self, text: str) -> None:
        self.host_status.setText(text)
        self.connect_status.setText(text)
        self.connection_badge.setText("● " + text)
        lowered = text.lower()
        if "подключ" in lowered:
            self.state.connected = True
        if "потеря" in lowered or "ошибка соединения" in lowered:
            self.state.connected = False
            if self.transfer_active:
                if self._transfer_sender is not None:
                    self._transfer_sender.cancel()
                if self._transfer_receiver is not None:
                    self._transfer_receiver.cancel()
                self._cleanup_transfer_state(send_cancel=False)
                self.player_status.setText("Соединение потеряно. Передача файла прервана.")
        self._update_transfer_controls()

    def _show_error(self, text: str) -> None:
        if text and text != "None":
            QMessageBox.warning(self, "SyncWatch", text)

    def _go_home(self) -> None:
        if self.transfer_active:
            self._cancel_file_transfer()
        self.player.stop()
        self.local_media = None
        self.remote_media = None
        self.local_media_path = None
        self._reset_media_match_state()
        self.state.local_ready = False
        self.state.remote_ready = False
        self._update_transfer_controls()
        self.stack.setCurrentWidget(self.home_page)

    @staticmethod
    def _local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"

    @staticmethod
    def _fmt(ms: int) -> str:
        seconds = max(0, ms // 1000)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape and self._fullscreen:
            self._exit_fullscreen()
            event.accept()
            return

        if self.stack.currentWidget() == self.player_page:
            key = event.key()
            if key == Qt.Key.Key_Space:
                self._toggle_playback()
                event.accept()
                return
            if key == Qt.Key.Key_Left:
                self._seek_relative(-5_000)
                event.accept()
                return
            if key == Qt.Key.Key_Right:
                self._seek_relative(5_000)
                event.accept()
                return
            if key == Qt.Key.Key_J:
                self._seek_relative(-10_000)
                event.accept()
                return
            if key == Qt.Key.Key_L:
                self._seek_relative(10_000)
                event.accept()
                return

        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.transfer_active:
            self._cancel_file_transfer()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.network.shutdown()
        super().closeEvent(event)
