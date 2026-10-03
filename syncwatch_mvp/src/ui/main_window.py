from __future__ import annotations

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.media.fingerprint import calculate_fingerprint
from src.media.metadata import MediaInfo, basic_file_data
from src.media.validator import media_matches
from src.network.file_transfer import FileTransferReceiver, FileTransferSender
from src.network.messages import Message
from src.network.network_thread import NetworkThread
from src.player.vlc_player import VlcPlayer
from src.session.role import Role
from src.session.state import PeerState, SessionState
from src.synchronization.drift_calculator import calculate_drift_ms, needs_correction
from src.ui.theme import APP_STYLE
from src.version import APP_VERSION


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
        self._ignore_release = False

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            if self._ignore_release:
                self._ignore_release = False
            else:
                self._single_click_timer.start()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._single_click_timer.stop()
            self._ignore_release = True
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class TransferSignals(QObject):
    progress = Signal(str, float, str)
    finished = Signal(str)
    failed = Signal(str, str)


class MainWindow(QMainWindow):
    def __init__(self, *, vlc_instance=None) -> None:
        super().__init__()
        self.setWindowTitle(f"SyncWatch {APP_VERSION}")
        self.resize(1080, 720)
        self.setMinimumSize(860, 600)
        self.setStyleSheet(APP_STYLE)
        self.state = SessionState()
        self.peers: dict[str, PeerState] = {}
        self._generation = 0
        self._load_generation = 0
        self._closing = False
        self._command_timers: list[QTimer] = []
        self._pending_command_id = 0
        self._clock_offset = 0.0
        self._clock_synced = False
        self._transfer_peer: str | None = None
        self._transfer_running = False
        self.transfer_timeout = QTimer(self)
        self.transfer_timeout.setSingleShot(True)
        self.transfer_timeout.setInterval(60000)
        self.transfer_timeout.timeout.connect(lambda: self._on_transfer_failed("Время ожидания передачи истекло"))
        self.network = NetworkThread()
        self.network.message_received.connect(self._on_message)
        self.network.status_changed.connect(self._set_status)
        self.network.failed.connect(self._network_failed)
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.command_id = 0
        self._last_broadcast_id = 0
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
        self._incoming_offer_peer: str | None = None

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
        self._set_volume(self.volume.value())
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
        subtitle = QLabel("Синхронный просмотр локального видео в одной комнате")
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

        footer = QLabel("Работает в локальной сети • можно передать видео участнику")
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
        subtitle = QLabel("Передайте IP, порт и код участникам.")
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
        cancel.clicked.connect(self._go_home)
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
        leave = QPushButton("Выйти из комнаты")
        leave.clicked.connect(self._go_home)
        header.addWidget(leave)
        layout.addWidget(self.player_header)

        self.video_card = QFrame()
        self.video_card.setObjectName("videoCard")
        video_layout = QVBoxLayout(self.video_card)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)

        self.video_surface = VideoSurface()
        self.video_surface.setMinimumHeight(300)
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
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self.force_match_button)
        self.recipient_combo = QComboBox()
        self.recipient_combo.setToolTip("Получатель файла")
        actions.addWidget(self.recipient_combo)
        actions.addWidget(self.send_file_button)
        actions.addWidget(self.cancel_transfer_button)
        actions.addWidget(choose)
        info_layout.addLayout(info)
        self.participants_label = QLabel()
        self.participants_label.setTextFormat(Qt.TextFormat.PlainText)
        self.participants_label.setWordWrap(True)
        self.participants_label.setObjectName("muted")
        info_layout.addWidget(self.participants_label)
        info_layout.addLayout(actions)
        self.player_status.setTextFormat(Qt.TextFormat.PlainText)

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
        self._go_home()
        self.state.role = Role.HOST
        self.host_info.setText("Создаём комнату…")
        self.stack.setCurrentWidget(self.host_page)
        self.network.host(45892)

    def _connect_room(self) -> None:
        ip = self.ip_edit.text().strip()
        code = self.code_edit.text().strip()
        name = self.name_edit.text().strip() or socket.gethostname()
        if not ip or not code or len(name) > 128:
            self._show_error("Введите IP, код комнаты и имя устройства до 128 символов")
            return
        self._go_home()
        self.state.role = Role.CLIENT
        self.stack.setCurrentWidget(self.connect_page)
        self.network.connect_to(ip, self.port_edit.value(), code, name)

    def _choose_media(self) -> None:
        if self.transfer_active:
            self._show_error("Дождитесь окончания передачи файла")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Выберите видео", "", "Видео (*.mp4 *.mkv *.mov *.avi *.m4v)")
        if not path:
            return
        self._start_media_load(path)

    def _invalidate_commands(self) -> None:
        self._generation += 1
        for timer in self._command_timers:
            timer.stop()
            timer.deleteLater()
        self._command_timers.clear()
        self._pending_command_id = 0

    def _start_media_load(self, path: str | Path) -> None:
        self._invalidate_commands()
        self._load_generation += 1
        generation = self._load_generation
        self.local_media = None
        self.state.local_ready = False
        self._reset_media_match_state()
        self.local_media_path = Path(path)
        self._announce_media()
        self.player_status.setText("Открытие файла и расчёт SHA-256…")
        try:
            self.player.open(str(path))
        except Exception as exc:
            self._media_failed((generation, str(exc)))
            return
        signals = WorkerSignals()
        signals.finished.connect(self._media_ready)
        signals.failed.connect(self._media_failed)
        deadline = time.monotonic() + 15

        def work():
            name, size = basic_file_data(path)
            fingerprint = calculate_fingerprint(path)
            if size != Path(path).stat().st_size:
                raise ValueError("Размер файла изменился во время проверки")
            return generation, MediaInfo(name, size, 0, fingerprint), deadline

        future = self.executor.submit(work)
        def done(result):
            if result.cancelled():
                return
            error = result.exception()
            if error is not None:
                signals.failed.emit((generation, str(error)))
            else:
                signals.finished.emit(result.result())
        future.add_done_callback(done)

    def _media_failed(self, result) -> None:
        generation, reason = result
        if generation != self._load_generation or self._closing:
            return
        self.player.stop()
        self.local_media = None
        self.state.local_ready = False
        self._reset_media_match_state()
        self._announce_media()
        self.player_status.setText(f"Не удалось открыть видео: {reason}")
        self._show_error(str(reason))

    def _media_ready(self, result) -> None:
        generation, media, deadline = result
        if generation != self._load_generation or self._closing:
            return
        if self.player.has_error():
            self._media_failed((generation, "VLC не поддерживает этот файл или файл повреждён"))
            return
        if not self.player.is_ready():
            if time.monotonic() > deadline:
                self._media_failed((generation, "VLC не подтвердил готовность видео за 15 секунд"))
            else:
                QTimer.singleShot(100, self, lambda: self._media_ready(result))
            return
        try:
            self.player.pause()
            self.player.seek(0)
            media.duration_ms = self.player.get_duration_ms()
        except Exception as exc:
            self._media_failed((generation, str(exc)))
            return
        self.local_media = media
        self.state.local_ready = True
        self.stack.setCurrentWidget(self.player_page)
        self._announce_media()
        self._check_media_match()
        self._update_transfer_controls()

    def _announce_media(self, recipient_id: str | None = None) -> None:
        if not self._is_connected():
            return
        if self.local_media and self.state.local_ready:
            self.network.send("media_info", self.local_media.to_dict(), recipient_id=recipient_id)
        self.network.send("ready" if self.state.local_ready else "not_ready",
                          {"media_loaded": self.state.local_ready}, recipient_id=recipient_id)

    def _reset_media_match_state(self) -> None:
        self.state.media_match = False
        self.state.media_force_match = False
        if hasattr(self, "force_match_button"):
            self.force_match_button.hide()

    def _check_media_match(self) -> None:
        self.participants_label.setText(" • ".join(f"{peer.name}: " +
            ("готов" if peer.ready else "выбирает видео") for peer in self.peers.values()))
        self.state.remote_ready = bool(self.peers) and all(peer.ready for peer in self.peers.values())
        media_present = bool(self.local_media and self.peers and all(peer.media for peer in self.peers.values()))
        exact = media_present and all(media_matches(self.local_media, peer.media) for peer in self.peers.values())
        self.state.media_match = bool(exact)
        if exact:
            self.state.media_force_match = False
        self.force_match_button.setVisible(bool(media_present and not exact and self.state.role == Role.HOST))
        ready = self.state.local_ready and self.state.remote_ready
        if ready and (exact or self.state.media_force_match):
            self.player_status.setText(f"Готовы к просмотру. Участников: {len(self.peers) + 1}." +
                                      (" Совпадение файлов разрешено вручную." if not exact else " Файлы совпадают."))
        elif media_present and not exact:
            self.player_status.setText("Файлы различаются. Ведущий может передать копию или разрешить просмотр без совпадения.")
        elif self.state.local_ready:
            self.player_status.setText("Видео готово. Ожидание подключения и готовности участников…")
        else:
            self.player_status.setText("Выберите видеофайл и дождитесь готовности участников.")
        self._update_transfer_controls()

    def _force_media_match(self) -> None:
        if self.state.role != Role.HOST or not self.local_media or not self.peers:
            return
        if not all(peer.media for peer in self.peers.values()):
            return
        self._apply_media_force_match(send_to_peer=True)

    def _apply_media_force_match(self, *, send_to_peer: bool) -> None:
        self.state.media_force_match = True
        self.force_match_button.hide()
        if send_to_peer:
            self.network.send("media_force_match")
        self._check_media_match()

    def _playback_allowed(self) -> bool:
        return bool(not self.transfer_active and self.state.connected and self.state.local_ready
                    and self.state.remote_ready and (self.state.media_match or self.state.media_force_match))

    def _is_connected(self) -> bool:
        return self.state.connected

    def _update_transfer_controls(self) -> None:
        if not hasattr(self, "send_file_button"):
            return
        current = self.recipient_combo.currentData()
        entries = [(peer_id, peer.name) for peer_id, peer in self.peers.items()]
        if [self.recipient_combo.itemData(i) for i in range(self.recipient_combo.count())] != [p[0] for p in entries]:
            self.recipient_combo.clear()
            for peer_id, name in entries:
                self.recipient_combo.addItem(name, peer_id)
            index = self.recipient_combo.findData(current)
            if index >= 0:
                self.recipient_combo.setCurrentIndex(index)
        self.recipient_combo.setVisible(self.state.role == Role.HOST and bool(entries))
        self.recipient_combo.setEnabled(not self.transfer_active)
        self.cancel_transfer_button.setVisible(self.transfer_active)
        self.transfer_progress.setVisible(self.transfer_active)
        self.send_file_button.setVisible(bool(self.local_media and self.local_media_path and self._is_connected()
                                              and not self.transfer_active))

    def _set_transfer_progress(self, fraction: float, text: str) -> None:
        self.transfer_progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))
        self.player_status.setText(text)

    def _start_file_offer(self) -> None:
        if self.transfer_active or not self.local_media or not self.local_media_path or not self._is_connected():
            return
        self._transfer_peer = self.recipient_combo.currentData() if self.state.role == Role.HOST else "host"
        if self._transfer_peer not in self.peers:
            self._show_error("Выберите подключённого получателя")
            return
        if self.player.is_playing():
            if self.state.role == Role.HOST:
                self._host_pause()
            else:
                self.player.pause()
        peer_id = self._transfer_peer
        is_host = self.state.role == Role.HOST
        def send(kind, payload):
            self.network.send_wait(kind, payload, recipient_id=peer_id if is_host else None)
        self._transfer_sender = FileTransferSender(self.local_media_path, self.local_media, send=send)
        self.transfer_active = True
        self._transfer_running = False
        self.transfer_timeout.start()
        self._update_transfer_controls()
        self.player_status.setText("Предложение отправить файл… Ожидание ответа.")
        self._send_transfer("file_offer", self._transfer_sender.offer_payload())

    def _send_transfer(self, kind: str, payload: dict) -> None:
        self.network.send(kind, payload, recipient_id=self._transfer_peer if self.state.role == Role.HOST else None)

    def _begin_outgoing_transfer(self) -> None:
        sender = self._transfer_sender
        if sender is None or self._transfer_running:
            return
        self._transfer_running = True
        token = sender.transfer_id
        signals = TransferSignals()
        signals.progress.connect(self._outgoing_progress)
        signals.finished.connect(self._on_outgoing_transfer_finished)
        signals.failed.connect(self._outgoing_failed)
        def work():
            try:
                sender.on_progress = lambda fraction, text: signals.progress.emit(token, fraction, text)
                sender.run()
                if not sender.cancel_event.is_set():
                    signals.finished.emit(token)
            except Exception as exc:
                signals.failed.emit(token, str(exc))
        self.executor.submit(work)

    def _outgoing_progress(self, token: str, fraction: float, text: str) -> None:
        if self._transfer_sender and self._transfer_sender.transfer_id == token:
            self.transfer_timeout.start()
            self._set_transfer_progress(fraction, text)

    def _outgoing_failed(self, token: str, reason: str) -> None:
        if self._transfer_sender and self._transfer_sender.transfer_id == token:
            self._on_transfer_failed(reason)

    def _on_outgoing_transfer_finished(self, token: str) -> None:
        if self._transfer_sender and self._transfer_sender.transfer_id == token:
            self.transfer_timeout.start()
            self.player_status.setText("Отправлено. Ожидание подтверждения целостности получателем…")

    def _on_transfer_failed(self, reason: str) -> None:
        self._cleanup_transfer_state(send_cancel=True)
        self.player_status.setText(reason or "Ошибка передачи файла")
        self._show_error(reason or "Ошибка передачи файла")
        self._update_transfer_controls()

    def _cancel_file_transfer(self) -> None:
        self._cleanup_transfer_state(send_cancel=True)
        self.player_status.setText("Передача файла отменена.")
        self._update_transfer_controls()

    def _cleanup_transfer_state(self, *, send_cancel: bool) -> None:
        sender, receiver = self._transfer_sender, self._transfer_receiver
        transfer_id = sender.transfer_id if sender else receiver.transfer_id if receiver else self._incoming_offer_id
        if send_cancel and transfer_id and self.state.connected:
            self._send_transfer("file_cancel", {"transfer_id": transfer_id})
        if sender:
            sender.cancel()
        if receiver:
            receiver.cancel_event.set()
            self.network.stop_receiving()
        self.transfer_timeout.stop()
        self.transfer_active = False
        self._transfer_running = False
        self._transfer_sender = None
        self._transfer_receiver = None
        self._incoming_offer_id = None
        self._incoming_offer_peer = None
        self._transfer_peer = None

    def _handle_file_offer(self, payload: dict, peer_id: str) -> None:
        transfer_id = payload["transfer_id"]
        target = peer_id if self.state.role == Role.HOST else None
        if self.transfer_active or self._incoming_offer_id:
            self.network.send("file_reject", {"transfer_id": transfer_id, "reason": "Уже идёт другая передача"}, recipient_id=target)
            return
        self._incoming_offer_id = transfer_id
        self._incoming_offer_peer = peer_id
        self._transfer_peer = peer_id
        generation = self._generation
        name, size = payload["file_name"], payload["file_size"]
        answer = QMessageBox.question(self, "Получение файла",
            f"{self.peers[peer_id].name} предлагает файл:\n\n{name}\nРазмер: {size / 1024**2:.1f} МБ\n\nПолучить и открыть видео?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if self._incoming_offer_id != transfer_id or generation != self._generation:
            return
        self._incoming_offer_id = None
        self._incoming_offer_peer = None
        if answer != QMessageBox.StandardButton.Yes:
            self.network.send("file_reject", {"transfer_id": transfer_id, "reason": "Отклонено пользователем"}, recipient_id=target)
            self._transfer_peer = None
            return
        try:
            receiver = FileTransferReceiver(payload)
        except Exception as exc:
            self.network.send("file_error", {"transfer_id": transfer_id, "reason": str(exc)}, recipient_id=target)
            self._transfer_peer = None
            self._show_error(str(exc))
            return
        self.player.pause()
        if self.state.role == Role.HOST:
            self._host_pause()
        self._transfer_receiver = receiver
        self.transfer_active = True
        self.transfer_timeout.start()
        self._update_transfer_controls()
        self.network.start_receiving(receiver, peer_id)
        self._set_transfer_progress(0.0, f"Получение «{name}»…")
        self._send_transfer("file_accept", {"transfer_id": transfer_id})

    def _on_message(self, message: Message) -> None:
        if self._closing or self.state.role == Role.NONE:
            return
        p, peer_id = message.payload, message.sender_id
        if message.type == "host_started" and self.state.role == Role.HOST:
            self.peers.clear()
            self.state.connected = False
            self._invalidate_commands()
            self._reset_media_match_state()
            self.state.session_id = message.session_id
            self.host_info.setText(f"IP: {self._local_ip()} • Порт: 45892 • Код: {p['room_code']}")
            return
        if message.type == "join_accepted" and self.state.role == Role.CLIENT:
            self.peers.clear()
            self._invalidate_commands()
            self._reset_media_match_state()
            self._clock_synced = False
            self.state.session_id = message.session_id
            self.state.connected = True
            self.peers["host"] = PeerState("Ведущий")
            self.stack.setCurrentWidget(self.player_page)
            self._announce_media()
            self._check_media_match()
            return
        if message.session_id != self.state.session_id:
            return
        if message.type == "peer_joined" and self.state.role == Role.HOST:
            self.peers[peer_id] = PeerState(p['device_name'])
            self.state.connected = True
            self.state.media_force_match = False
            if self.player.is_playing():
                self._host_pause()
            self._announce_media(recipient_id=peer_id)
            self.network.send("ping", recipient_id=peer_id)
            self._check_media_match()
            return
        if message.type == "peer_left":
            self.peers.pop(peer_id, None)
            self.state.connected = bool(self.peers)
            self.state.media_force_match = False
            if self._transfer_peer == peer_id or self._incoming_offer_peer == peer_id:
                self._cleanup_transfer_state(send_cancel=False)
            self._invalidate_commands()
            self.player.pause()
            if self.state.role == Role.HOST and self.peers and self.local_media:
                self._host_pause()
            self._check_media_match()
            return
        if peer_id not in self.peers:
            return
        peer = self.peers[peer_id]
        if message.type == "media_info":
            peer.media = MediaInfo.from_dict(p)
            peer.ready = False
            self.remote_media = peer.media
            self.state.media_force_match = False
            self._invalidate_commands()
            self.player.pause()
            self._check_media_match()
        elif message.type in {"ready", "not_ready"}:
            peer.ready = p['media_loaded'] and peer.media is not None
            if not peer.ready:
                self._invalidate_commands()
                self.state.media_force_match = False
                self.player.pause()
                if self.state.role == Role.HOST and self.local_media:
                    self._host_pause()
            self._check_media_match()
        elif message.type == "media_force_match" and self.state.role == Role.CLIENT:
            self._apply_media_force_match(send_to_peer=False)
        elif message.type in {"play", "pause", "seek", "sync_correction"}:
            if self.state.role == Role.CLIENT:
                self._schedule_remote_command(message)
        elif message.type == "state" and self.state.role == Role.HOST:
            self._handle_remote_state(message)
        elif message.type == "pong":
            now = time.monotonic()
            rtt = now - p['echo']
            if not 0 <= rtt <= 10:
                return
            peer.ping_ms = rtt * 1000
            peer.clock_offset = p['remote_time'] - (p['echo'] + now) / 2
            self.state.ping_ms = max((peer.ping_ms for peer in self.peers.values()), default=0)
            if self.state.role == Role.CLIENT:
                self._clock_offset = peer.clock_offset
                self._clock_synced = True
        elif message.type == "file_offer":
            self._handle_file_offer(p, peer_id)
        elif message.type.startswith("file_"):
            sender, receiver = self._transfer_sender, self._transfer_receiver
            token = sender.transfer_id if sender else receiver.transfer_id if receiver else self._incoming_offer_id
            if peer_id != self._transfer_peer or p.get("transfer_id") != token:
                return
            if message.type == "file_accept" and sender:
                self.transfer_timeout.start()
                self._begin_outgoing_transfer()
            elif message.type == "file_progress" and receiver:
                self.transfer_timeout.start()
                self._set_transfer_progress(p['fraction'], f"Получение {int(p['fraction'] * 100)}%…")
            elif message.type == "file_saved" and receiver:
                self._send_transfer("file_received", {"transfer_id": token})
                path = p['path']
                self._cleanup_transfer_state(send_cancel=False)
                self._start_media_load(path)
            elif message.type == "file_received" and sender:
                self._cleanup_transfer_state(send_cancel=False)
                self.player_status.setText("Получатель подтвердил SHA-256. Ожидание готовности видео…")
            elif message.type in {"file_cancel", "file_reject", "file_error"}:
                self._cleanup_transfer_state(send_cancel=False)
                self.player_status.setText(p.get('reason', "Передача отменена другим участником"))
                self._update_transfer_controls()

    def _schedule_remote_command(self, message: Message) -> None:
        if self.state.role != Role.CLIENT or not self.state.connected or not self.state.local_ready:
            return
        if not self.remote_media or message.payload.get("media_fingerprint") != self.remote_media.fingerprint:
            return
        if not (self.state.media_match or self.state.media_force_match):
            return
        self._queue_command(message, remote=True)

    def _queue_command(self, message: Message, *, remote: bool = False) -> None:
        command_id = message.payload["command_id"]
        if command_id <= self.last_command_id:
            return
        self.last_command_id = command_id
        for timer in self._command_timers:
            timer.stop()
            timer.deleteLater()
        self._command_timers.clear()
        now = time.monotonic()
        if remote and self._clock_synced:
            deadline = float(message.payload["execute_at"]) - self._clock_offset
        elif remote:
            deadline = message.received_at + max(0, message.payload["execute_delay_ms"] / 1000 - self.state.ping_ms / 2000)
        else:
            deadline = float(message.payload["execute_at"])
        delay_ms = max(0, min(5000, round((deadline - now) * 1000)))
        timer = QTimer(self)
        timer.setSingleShot(True)
        epoch = self._generation
        self._pending_command_id = command_id

        def execute():
            if timer in self._command_timers:
                self._command_timers.remove(timer)
            timer.deleteLater()
            if self._closing or epoch != self._generation or command_id != self.last_command_id:
                return
            self._pending_command_id = 0
            if message.type != "pause" and not self._playback_allowed():
                return
            try:
                lateness = max(0, round((time.monotonic() - deadline) * 1000))
                self._execute_command(message, lateness=lateness)
            except Exception as exc:
                self._show_error(str(exc))
        timer.timeout.connect(execute)
        self._command_timers.append(timer)
        timer.start(delay_ms)

    def _execute_command(self, message: Message, *, lateness: int = 0) -> None:
        position = message.payload["position_ms"]
        resume = message.type == "play" or bool(message.payload.get("resume", False))
        if resume:
            position += lateness
        if message.type == "pause":
            self.player.pause()
            self.player.seek(position)
            self.controls_hide_timer.stop()
            self._show_player_controls(restart_timer=False)
        else:
            self.player.seek(position)
            if resume:
                self.player.play()
            else:
                self.player.pause()
            self._show_player_controls()

    def _broadcast_command(self, kind: str, position: int, resume: bool = False) -> None:
        delay_ms = max(350, min(1500, int(self.state.ping_ms * 3)))
        command = {
            "command_id": self._next_command(), "position_ms": max(0, position),
            "resume": resume, "execute_delay_ms": delay_ms,
            "execute_at": time.monotonic() + delay_ms / 1000,
            "media_fingerprint": self.local_media.fingerprint if self.local_media else "",
        }
        self._last_broadcast_id = command["command_id"]
        self.network.send(kind, command)
        self._queue_command(Message(kind, command))

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
                "Дождитесь подключения и готовности всех участников. Если файлы различаются, разрешите просмотр без совпадения."
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
        if self.state.role == Role.HOST and self._playback_allowed():
            self._broadcast_command("play", self.player.get_position_ms(), True)

    def _host_pause(self) -> None:
        if self.state.role != Role.HOST or not self.local_media:
            return
        delay_ms = max(350, min(1500, int(self.state.ping_ms * 3)))
        position = self.player.get_position_ms() + (delay_ms if self.player.is_playing() else 0)
        self._broadcast_command("pause", position)

    def _seek_relative(self, delta_ms: int) -> None:
        if self.state.role != Role.HOST or not self._playback_allowed():
            return
        duration = self.player.get_duration_ms()
        target = max(0, min(max(0, duration - 1), self.player.get_position_ms() + delta_ms))
        self._broadcast_command("seek", target, self.player.is_playing())

    def _timeline_pressed(self) -> None:
        self.dragging = True
        self.controls_hide_timer.stop()
        self._show_player_controls(restart_timer=False)

    def _timeline_released(self) -> None:
        self.dragging = False
        self._show_player_controls()
        if self.state.role == Role.HOST and self._playback_allowed():
            duration = self.player.get_duration_ms()
            target = int(max(0, duration - 1) * self.timeline.value() / 1000)
            self._broadcast_command("seek", target, self.player.is_playing())

    def _send_state(self) -> None:
        if not self.state.connected:
            return
        self.network.send("ping")
        if self.state.role == Role.CLIENT and self.state.local_ready:
            self.network.send("state", {
                "position_ms": self.player.get_position_ms(), "playing": self.player.is_playing(),
                "last_command_id": self.last_command_id, "local_time": time.monotonic(),
            })

    def _handle_remote_state(self, message: Message) -> None:
        peer = self.peers.get(message.sender_id)
        if not peer or not peer.ready or not self._playback_allowed() or self._pending_command_id:
            return
        if message.payload.get("last_command_id", 0) < self._last_broadcast_id:
            return
        now = time.monotonic()
        playing = self.player.is_playing()
        remote_playing = message.payload["playing"]
        # Peer clock offset is remote_time - host_time, measured with pong.
        age = max(0, min(2.0, now - (message.sent_at - peer.clock_offset)))
        remote = message.payload["position_ms"] + (round(age * 1000) if remote_playing else 0)
        expected = self.player.get_position_ms()
        drift = calculate_drift_ms(expected, remote)
        peer.drift_ms = drift
        self.state.drift_ms = max((p.drift_ms for p in self.peers.values()), key=abs, default=0)
        if (needs_correction(drift) or playing != remote_playing) and now - peer.last_correction >= 3:
            peer.last_correction = now
            delay_ms = max(350, min(1500, int(peer.ping_ms * 3)))
            self.network.send("sync_correction", {
                "command_id": self._next_command(),
                "position_ms": expected + (delay_ms if playing else 0), "resume": playing,
                "execute_delay_ms": delay_ms, "execute_at": now + delay_ms / 1000,
                "media_fingerprint": self.local_media.fingerprint,
            }, recipient_id=message.sender_id)

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

    def _network_failed(self, text: str) -> None:
        self._set_status(f"Ошибка соединения: {text}")
        if self.transfer_active:
            self._cleanup_transfer_state(send_cancel=False)
        self._show_error(text)

    def _show_error(self, text: str) -> None:
        if text and text != "None" and not self._closing:
            logging.warning("UI: %s", text)
            QMessageBox.warning(self, "SyncWatch", text)

    def _go_home(self) -> None:
        if self.transfer_active:
            self._cancel_file_transfer()
        self._incoming_offer_id = None
        self._incoming_offer_peer = None
        self._invalidate_commands()
        self._load_generation += 1
        self.network.leave_room()
        self.player.stop()
        if self._fullscreen:
            self._exit_fullscreen()
        self.peers.clear()
        self.state = SessionState()
        self.command_id = 0
        self._last_broadcast_id = 0
        self.last_command_id = 0
        self._clock_synced = False
        self.local_media = None
        self.remote_media = None
        self.local_media_path = None
        self._reset_media_match_state()
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
        self._closing = True
        self.ui_timer.stop()
        self.state_timer.stop()
        self._go_home()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.network.shutdown()
        self.player.release()
        super().closeEvent(event)
