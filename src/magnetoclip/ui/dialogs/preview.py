"""PreviewDialog: view images, text, video, and audio inside the app.

PDF previews fall back to opening the file with the system's default
application since Qt ships no embedded PDF viewer.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QWheelEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from magnetoclip.services.preview.resolver import PreviewType

from ..util import format_bytes, open_path

# ── palette tokens ─────────────────────────────────────────────────────────
_SURFACE = "#131624"
_SURFACE_ALT = "#1A1E31"
_BORDER = "#262B44"
_TEXT_MUTED = "#8A92B5"
_ACCENT = "#8B5CF6"


class _MediaControlButton(QAbstractButton):
    """Square media icon button with hover, click "pop", and glow animations."""

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._hovered = False
        self._glow = False
        self._pulse = 0.0
        self._pop = 1.0
        self.setFixedSize(44, 38)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip({"play": "Play", "pause": "Pause", "stop": "Stop"}[kind])

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

        self._pop_anim = QPropertyAnimation(self, b"pop", self)
        self._pop_anim.setDuration(220)
        self._pop_anim.setEasingCurve(QEasingCurve.OutBack)

    # -- pop property (driven by QPropertyAnimation) -------------------------

    def _get_pop(self) -> float:
        return self._pop

    def _set_pop(self, v: float) -> None:
        self._pop = v
        self.update()

    pop = property(_get_pop, _set_pop)

    def sizeHint(self) -> QSize:
        return QSize(44, 38)

    # -- animation ------------------------------------------------------------

    def _tick(self) -> None:
        self._pulse += 0.18
        if self._pulse > math.tau:
            self._pulse -= math.tau
        self.update()

    def set_glow(self, on: bool) -> None:
        if on == self._glow:
            return
        self._glow = on
        if on:
            self._pulse = 0.0
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    # -- events ---------------------------------------------------------------

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._pop = 1.25
        self._pop_anim.stop()
        self._pop_anim.setStartValue(1.25)
        self._pop_anim.setEndValue(1.0)
        self._pop_anim.start()
        super().mousePressEvent(event)

    # -- painting -------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(3, 2, -3, -2)

        if self._hovered or self._glow:
            fill = QColor(_ACCENT)
            fill.setAlpha(30 if not self._hovered else 55)
            p.setBrush(fill)
            p.setPen(QPen(QColor(_ACCENT), 1))
        else:
            p.setBrush(QColor(_SURFACE_ALT))
            p.setPen(QPen(QColor(_BORDER), 1))
        p.drawRoundedRect(rect, 8, 8)

        center = rect.center()
        if self._glow:
            glow = QColor(_ACCENT)
            glow.setAlpha(int(40 + 70 * (0.5 + 0.5 * math.sin(self._pulse))))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(glow, 2))
            radius = rect.width() * (0.55 + 0.12 * math.sin(self._pulse))
            p.drawEllipse(center, radius, radius)

        self._draw_glyph(p, center)

        p.end()

    def _glyph_color(self) -> QColor:
        if self._hovered or self._glow:
            return QColor(_ACCENT)
        return QColor(_TEXT_MUTED)

    def _draw_glyph(self, p: QPainter, c) -> None:
        color = self._glyph_color()
        s = 15 * self._pop
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        if self._kind == "play":
            triangle = QPolygonF(
                [
                    QPointF(c.x() - s * 0.32, c.y() - s * 0.55),
                    QPointF(c.x() - s * 0.32, c.y() + s * 0.55),
                    QPointF(c.x() + s * 0.62, c.y()),
                ]
            )
            p.drawPolygon(triangle)
        elif self._kind == "pause":
            bar_w = s * 0.24
            bar_h = s * 0.75
            p.drawRoundedRect(
                QRectF(c.x() - s * 0.5, c.y() - bar_h / 2, bar_w, bar_h), 2, 2
            )
            p.drawRoundedRect(
                QRectF(c.x() + s * 0.5 - bar_w, c.y() - bar_h / 2, bar_w, bar_h), 2, 2
            )
        elif self._kind == "stop":
            size = s * 0.62
            p.drawRoundedRect(
                QRectF(c.x() - size / 2, c.y() - size / 2, size, size), 3, 3
            )


class PreviewDialog(QDialog):
    def __init__(self, context, path: str, preview_type: PreviewType, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.path = path
        self.preview_type = preview_type
        self.player: QMediaPlayer | None = None
        self.setWindowTitle(f"Preview — {Path(path).name}")
        self.resize(900, 640)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self.fit_button = QPushButton("Fit")
        self.zoom_in_button = QPushButton("+")
        self.zoom_out_button = QPushButton("−")
        self.open_button = QPushButton("Open Externally")
        for btn in (
            self.fit_button,
            self.zoom_in_button,
            self.zoom_out_button,
            self.open_button,
        ):
            btn.setProperty("role", "ghost")
            toolbar.addWidget(btn)
        toolbar.addStretch(1)
        close_button = QPushButton("Close")
        close_button.setProperty("role", "ghost")
        close_button.clicked.connect(self.accept)
        toolbar.addWidget(close_button)
        layout.addLayout(toolbar)

        if preview_type == PreviewType.IMAGE:
            self._build_image(layout)
            self.zoom_in_button.clicked.connect(lambda: self._zoom(1.2))
            self.zoom_out_button.clicked.connect(lambda: self._zoom(0.8))
            self.fit_button.clicked.connect(self._fit)
        elif preview_type == PreviewType.TEXT:
            self._build_text(layout)
            self.zoom_in_button.hide()
            self.zoom_out_button.hide()
            self.fit_button.hide()
        elif preview_type == PreviewType.VIDEO:
            self._build_video(layout)
            self.zoom_in_button.hide()
            self.zoom_out_button.hide()
            self.fit_button.hide()
        elif preview_type == PreviewType.AUDIO:
            self._build_audio(layout)
            self.zoom_in_button.hide()
            self.zoom_out_button.hide()
            self.fit_button.hide()
        elif preview_type == PreviewType.TORRENT:
            self._build_torrent(layout)
            self.zoom_in_button.hide()
            self.zoom_out_button.hide()
            self.fit_button.hide()
        else:
            self._build_fallback(layout)
            self.zoom_in_button.hide()
            self.zoom_out_button.hide()
            self.fit_button.hide()

        self.open_button.clicked.connect(self._open_external)

    # ----- image -----

    def _build_image(self, layout: QVBoxLayout) -> None:
        self._zoom_factor = 1.0
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(400, 300)
        self.pixmap = QPixmap(self.path)
        self.image_label.setPixmap(self.pixmap)
        self.scroll.setWidget(self.image_label)
        layout.addWidget(self.scroll, 1)

    def _zoom(self, factor: float) -> None:
        self._zoom_factor *= factor
        if self.pixmap.isNull():
            return
        scaled = self.pixmap.scaled(
            int(self.pixmap.width() * self._zoom_factor),
            int(self.pixmap.height() * self._zoom_factor),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def _fit(self) -> None:
        if not self.pixmap.isNull():
            self.image_label.setPixmap(
                self.pixmap.scaled(
                    self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.preview_type == PreviewType.IMAGE:
            delta = event.angleDelta().y()
            self._zoom(1.15 if delta > 0 else 0.85)
        super().wheelEvent(event)

    # ----- text -----

    def _build_text(self, layout: QVBoxLayout) -> None:
        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        try:
            content = Path(self.path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            content = f"Unable to read file: {exc}"
        self.text_view.setPlainText(content)
        layout.addWidget(self.text_view, 1)

    # ----- video -----

    def _build_video(self, layout: QVBoxLayout) -> None:
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.video_widget = QVideoWidget()
        self.player.setVideoOutput(self.video_widget)
        self.player.setSource(QUrl.fromLocalFile(self.path))

        layout.addWidget(self.video_widget, 1)

        self._build_media_controls(layout)

        self.player.playbackStateChanged.connect(self._update_media_controls)
        self.player.positionChanged.connect(lambda _: self._on_position_changed())
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.errorOccurred.connect(self._on_player_error)
        self.player.play()

    # ----- audio -----

    def _build_audio(self, layout: QVBoxLayout) -> None:
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setSource(QUrl.fromLocalFile(self.path))

        album = QLabel(Path(self.path).name)
        album.setObjectName("page_subtitle")
        album.setAlignment(Qt.AlignCenter)
        album.setStyleSheet("font-size: 20px; font-weight: 600; margin-top: 40px;")
        layout.addWidget(album)

        self._build_media_controls(layout)
        layout.addStretch(1)

        self.player.playbackStateChanged.connect(self._update_media_controls)
        self.player.positionChanged.connect(lambda _: self._on_position_changed())
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.errorOccurred.connect(self._on_player_error)

    # ----- shared media scaffolding -----

    def _build_media_controls(self, layout: QVBoxLayout) -> None:
        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(
            lambda pos: self.player.setPosition(pos)
        )
        layout.addWidget(self.position_slider)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.play_button = _MediaControlButton("play")
        self.play_button.clicked.connect(self._play_pressed)
        self.pause_button = _MediaControlButton("pause")
        self.pause_button.clicked.connect(self._pause_pressed)
        self.stop_button = _MediaControlButton("stop")
        self.stop_button.clicked.connect(self._stop_pressed)
        for btn in (self.play_button, self.pause_button, self.stop_button):
            controls.addWidget(btn)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.media_time_label = QLabel("0:00 / 0:00")
        self.media_time_label.setObjectName("card_caption")
        self.media_time_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.media_time_label)

    def _play_pressed(self) -> None:
        if self.player is not None:
            self.player.play()

    def _pause_pressed(self) -> None:
        if self.player is not None:
            self.player.pause()

    def _stop_pressed(self) -> None:
        if self.player is not None:
            self.player.stop()
            self.position_slider.setValue(0)

    def _on_duration_changed(self, duration: int) -> None:
        if duration >= 0:
            self.position_slider.setRange(0, int(duration))
            enabled = duration > 0
            self.play_button.setEnabled(enabled)
            self.pause_button.setEnabled(enabled)
            self.stop_button.setEnabled(enabled)
        self._update_media_controls()

    def _update_media_controls(self) -> None:
        if self.player is None:
            return
        state = self.player.playbackState()
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        position = self.player.position()
        self.play_button.set_glow(not playing and position == 0 and self.player.duration() > 0)
        self.pause_button.set_glow(playing)
        self.stop_button.set_glow(position > 0)

    def _on_position_changed(self) -> None:
        if self.player is None:
            return
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(int(self.player.position()))
        duration = self.player.duration()
        if duration > 0:
            self.media_time_label.setText(
                f"{_fmt_time(self.player.position())} / {_fmt_time(duration)}"
            )
        self._update_media_controls()

    def _on_player_error(self, error, error_string: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        self.media_time_label.setText(
            f"Playback unavailable: {error_string or str(error)}"
        )

    # ----- fallback (pdf) -----

    def _build_fallback(self, layout: QVBoxLayout) -> None:
        label = QLabel(
            f"Embedded preview for {self.preview_type.value} files is not bundled.\n"
            "Use 'Open Externally' to view with your system application."
        )
        label.setAlignment(Qt.AlignCenter)
        label.setObjectName("card_caption")
        layout.addWidget(label, 1)

    # ----- torrent -----

    def _build_torrent(self, layout: QVBoxLayout) -> None:
        from magnetoclip.torrent.parser import parse_torrent_file

        try:
            meta = parse_torrent_file(self.path)
        except Exception:  # noqa: BLE001 - invalid files fall through to the error view
            meta = None

        if meta is None or (not meta.info_hash and meta.total_size == 0):
            label = QLabel(
                "This does not appear to be a valid .torrent file.\n"
                "Use 'Open Externally' to open it with your system application."
            )
            label.setAlignment(Qt.AlignCenter)
            label.setObjectName("card_caption")
            layout.addWidget(label, 1)
            return

        name = QLabel(meta.name or Path(self.path).name)
        name.setObjectName("page_title")
        name.setWordWrap(True)
        layout.addWidget(name)

        if meta.comment:
            comment = QLabel(meta.comment)
            comment.setObjectName("card_caption")
            comment.setWordWrap(True)
            layout.addWidget(comment)

        hash_row = QHBoxLayout()
        hash_label = QLabel(f"Info hash:  {meta.info_hash}")
        hash_label.setObjectName("card_caption")
        hash_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        copy_btn = QPushButton("Copy Hash")
        copy_btn.setProperty("role", "ghost")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(meta.info_hash)
        )
        hash_row.addWidget(hash_label, 1)
        hash_row.addWidget(copy_btn)
        layout.addLayout(hash_row)

        if meta.tracker_url:
            tracker = QLabel(f"Tracker:  {meta.tracker_url}")
            tracker.setObjectName("card_caption")
            tracker.setWordWrap(True)
            tracker.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(tracker)

        if meta.created_by:
            created = QLabel(f"Created by:  {meta.created_by}")
            created.setObjectName("card_caption")
            layout.addWidget(created)

        fields_title = QLabel("Contents")
        fields_title.setObjectName("card_title")
        layout.addWidget(fields_title)

        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["File", "Size"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setFocusPolicy(Qt.NoFocus)

        if meta.files:
            entries = [(f.path, f.size) for f in meta.files]
        else:
            entries = [(meta.name or Path(self.path).name, meta.total_size)]

        table.setRowCount(len(entries))
        for row, (path, size) in enumerate(entries):
            name_item = QTableWidgetItem(path)
            size_item = QTableWidgetItem(format_bytes(size))
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, size_item)
        self.file_table = table
        layout.addWidget(table, 1)

        footer = QLabel(
            f"{meta.file_count} file{'s' if meta.file_count != 1 else ''} • "
            f"{format_bytes(meta.total_size)} total"
        )
        footer.setObjectName("card_caption")
        layout.addWidget(footer)

    def _open_external(self) -> None:
        if not open_path(self.path):
            QMessageBox.warning(self, "Preview", "No application is associated with this file.")

    def closeEvent(self, event) -> None:
        if self.player is not None:
            self.player.stop()
        super().closeEvent(event)


def _fmt_time(ms: int) -> str:
    total_seconds = max(0, ms) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"