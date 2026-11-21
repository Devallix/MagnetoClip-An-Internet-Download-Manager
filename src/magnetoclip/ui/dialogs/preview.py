"""PreviewDialog: view images, text, video, and audio inside the app.

PDF previews fall back to opening the file with the system's default
application since Qt ships no embedded PDF viewer.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtMultimedia import (
    QAudioOutput,
    QMediaPlayer,
    QVideoFrame,
    QVideoSink,
)
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
    QSizePolicy,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.media.ffmpeg import FFmpegLocator
from magnetoclip.media.streaming import DownloadCancelled
from magnetoclip.services.logging.setup import get_logger
from magnetoclip.services.preview import proxy as preview_proxy
from magnetoclip.services.preview.resolver import PreviewType

from ..util import format_bytes, open_path

log = get_logger(__name__)

# How long playback gets to produce its first video frame before the preview
# assumes the codec is undecodable and falls back to a transcoded proxy. Kept
# generous so a large file on slow storage is not mistaken for a bad codec.
_FRAME_GRACE_MS = 2500


class _CodecCheckWorker(QThread):
    """Probes a source's video codec off the UI thread before playback starts.

    Handing a file with an undecodable codec to ``QMediaPlayer`` still produces
    audio, but it also floods the log with per-frame decoder failures and shows
    a black surface. Checking first lets the preview skip that entirely.
    """

    verdict = Signal(str)

    def __init__(self, *, source: str, context) -> None:
        super().__init__()
        self._source = source
        self._context = context
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            import asyncio

            from magnetoclip.media.ffmpeg import probe

            info = asyncio.run(
                probe(
                    self._source,
                    FFmpegLocator(settings=getattr(self._context, "settings", None)),
                )
            )
        except Exception as exc:  # noqa: BLE001 - fall back to normal playback
            log.warning("preview_probe_failed", error=str(exc))
            self.verdict.emit("playable")
            return
        if self._cancel.is_set():
            return
        codec = (info or {}).get("codec")
        self.verdict.emit("unsupported" if preview_proxy.needs_proxy(codec) else "playable")


class _PreviewProxyWorker(QThread):
    """Transcodes a source into a playable proxy off the UI thread."""

    progress = Signal(float)
    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, *, source: str, context, destination) -> None:
        super().__init__()
        self._source = source
        self._context = context
        self._destination = destination
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            from magnetoclip.media.ffmpeg import create_preview_proxy

            path = create_preview_proxy(
                self._source,
                self._destination,
                locator=FFmpegLocator(
                    settings=getattr(self._context, "settings", None)
                ),
                max_seconds=preview_proxy.PROXY_MAX_SECONDS,
                max_width=preview_proxy.PROXY_MAX_WIDTH,
                progress_cb=self.progress.emit,
                cancel_event=self._cancel,
            )
        except DownloadCancelled:
            return
        except Exception as exc:  # noqa: BLE001 - reported through failed()
            log.warning("preview_proxy_failed", error=str(exc))
            self.failed.emit(str(exc))
            return
        if not self._cancel.is_set():
            self.ready.emit(str(path))

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


class _VideoSurface(QWidget):
    """Paints the most recent decoded frame, letterboxed and aspect-fitted.

    ``QVideoWidget`` renders through a composition path that never reaches the
    widget on the affected Qt 6.11 Windows build, so a previewed clip showed a
    black rectangle while its audio played. Painting the frame ourselves keeps
    the ordinary widget repaint pipeline, which does work.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._frame: QImage | None = None
        self.paints_with_frame = 0
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    def has_frame(self) -> bool:
        return self._frame is not None and not self._frame.isNull()

    def set_frame(self, frame: QVideoFrame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        self._frame = image
        self.update()

    def clear_frame(self) -> None:
        self._frame = None
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        image = self._frame
        if image is not None and not image.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            scaled = image.scaled(
                self.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            painter.drawImage(
                QPointF(
                    (self.width() - scaled.width()) / 2,
                    (self.height() - scaled.height()) / 2,
                ),
                scaled,
            )
            self.paints_with_frame += 1
        painter.end()


class _VideoFrameSink(QVideoSink):
    """Forwards decoded frames from ``QVideoSink`` onto a :class:`_VideoSurface`.

    The frames arrive on the ``videoFrameChanged`` signal rather than through an
    ``onVideoFrameChanged`` override: PySide6 does not dispatch that virtual, so
    an override runs silently and the surface never updates.
    """

    def __init__(self, surface: _VideoSurface) -> None:
        super().__init__()
        self._surface = surface
        self.frames = 0
        self.videoFrameChanged.connect(self._on_frame)

    def _on_frame(self, frame: QVideoFrame) -> None:
        self.frames += 1
        self._surface.set_frame(frame)


class PreviewDialog(QDialog):
    def __init__(self, context, path: str, preview_type: PreviewType, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.path = path
        self.preview_type = preview_type
        self.player: QMediaPlayer | None = None
        self._autoplay = False
        self._proxy_worker: _PreviewProxyWorker | None = None
        self._check_worker: _CodecCheckWorker | None = None
        self._proxy_timer: QTimer | None = None
        self._proxy_tried = False
        self._playback_started = False
        self._proxy_path: str | None = None
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
        self.video_surface = _VideoSurface()
        self.video_sink = _VideoFrameSink(self.video_surface)
        self.player.setVideoOutput(self.video_sink)
        self.player.setSource(QUrl.fromLocalFile(self.path))

        layout.addWidget(self.video_surface, 1)

        self._build_media_controls(layout)

        self.player.playbackStateChanged.connect(self._update_media_controls)
        self.player.positionChanged.connect(lambda _: self._on_position_changed())
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.errorOccurred.connect(self._on_player_error)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        # Playback starts from showEvent: starting here would run before the
        # surface has a paintable geometry.
        self._autoplay = True

    # ----- undecodable video fallback -----

    def _begin_playback(self) -> None:
        """Start playback, transcoding first if the codec cannot be decoded."""
        if self._playback_started:
            return
        self._playback_started = True
        cached = preview_proxy.cached_proxy(self.path, self.context)
        if cached is not None:
            self._use_proxy(cached, note="Showing a cached compatible preview.")
            return
        # A cached proxy is not always present, and the probe is cheap, so ask
        # ffprobe what the video codec is before letting Qt try to decode it.
        # The watchdog still runs underneath as a safety net for codecs that
        # ffprobe reports as fine but that Qt turns out not to decode.
        self._check_worker = _CodecCheckWorker(source=self.path, context=self.context)
        self._check_worker.verdict.connect(self._on_codec_verdict)
        self._check_worker.finished.connect(self._forget_check_worker)
        self._check_worker.start()
        self._arm_frame_watchdog()

    def _on_codec_verdict(self, verdict: str) -> None:
        if verdict == "unsupported":
            self._maybe_build_proxy()
            return
        if self.player is not None:
            self.player.play()

    def _forget_check_worker(self) -> None:
        worker = self._check_worker
        self._check_worker = None
        if worker is not None:
            worker.deleteLater()

    def _arm_frame_watchdog(self) -> None:
        """Fall back to a transcoded proxy if the video track yields no frames.

        Some codecs decode to nothing in Qt's bundled FFmpeg, so the file plays
        audio over a black surface. Waiting for frames rather than trusting the
        codec name means any undecodable track is handled, not only the ones
        known today.
        """
        if self._proxy_timer is not None or self._proxy_tried:
            return
        self._proxy_timer = QTimer(self)
        self._proxy_timer.setSingleShot(True)
        self._proxy_timer.timeout.connect(self._maybe_build_proxy)
        self._proxy_timer.start(_FRAME_GRACE_MS)

    def _maybe_build_proxy(self) -> None:
        if self.video_sink.frames > 0 or self._proxy_tried:
            return
        self._proxy_tried = True
        if self._proxy_timer is not None:
            self._proxy_timer.stop()
            self._proxy_timer = None
        cached = preview_proxy.cached_proxy(self.path, self.context)
        if cached is not None:
            self._use_proxy(cached, note="Showing a cached compatible preview.")
            return
        try:
            locator = FFmpegLocator(
                settings=getattr(self.context, "settings", None)
            )
            if not locator.ffmpeg_available:
                self._on_undecodable_video("ffmpeg was not found on this system")
                return
        except Exception as exc:  # noqa: BLE001 - never block the dialog
            self._on_undecodable_video(f"ffmpeg is unavailable: {exc}")
            return

        self._on_proxy_progress(0.0)
        self._proxy_worker = _PreviewProxyWorker(
            source=self.path,
            context=self.context,
            destination=preview_proxy.proxy_path(self.path, self.context),
        )
        self._proxy_worker.progress.connect(self._on_proxy_progress)
        self._proxy_worker.ready.connect(self._use_proxy)
        self._proxy_worker.failed.connect(self._on_undecodable_video)
        self._proxy_worker.finished.connect(self._forget_proxy_worker)
        self._proxy_worker.start()

    def _on_proxy_progress(self, seconds: float) -> None:
        self.media_error_label.setText(
            "This codec cannot be previewed directly — preparing a "
            f"compatible version… {int(seconds)}s"
        )
        self.media_error_label.show()

    def _use_proxy(self, path, note: str | None = None) -> None:
        if self.player is None:
            return
        self._proxy_path = str(path)
        self._clear_playback_error()
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(self._proxy_path))
        self.player.play()
        if note:
            self.media_error_label.setText(note)
            self.media_error_label.show()

    def _on_undecodable_video(self, reason: str) -> None:
        self._on_player_error(
            QMediaPlayer.Error.FormatError,
            f"The video track could not be decoded ({reason}). "
            "Use 'Open Externally' to view it in another player.",
        )

    def _forget_proxy_worker(self) -> None:
        worker = self._proxy_worker
        self._proxy_worker = None
        if worker is not None:
            worker.deleteLater()

    def _cancel_proxy(self) -> None:
        if self._proxy_timer is not None:
            self._proxy_timer.stop()
            self._proxy_timer = None
        for worker in (self._check_worker, self._proxy_worker):
            if worker is None:
                continue
            worker.request_cancel()
            worker.wait(3000)
        self._check_worker = None
        self._proxy_worker = None

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

        # Errors get their own label. They used to be written to the elapsed
        # time label, which refreshes several times a second and therefore
        # wiped the message before it could be read.
        self.media_error_label = QLabel()
        self.media_error_label.setObjectName("card_caption")
        self.media_error_label.setAlignment(Qt.AlignCenter)
        self.media_error_label.setWordWrap(True)
        self.media_error_label.hide()
        layout.addWidget(self.media_error_label)

    def _clear_playback_error(self) -> None:
        label = getattr(self, "media_error_label", None)
        if label is not None:
            label.clear()
            label.hide()

    def _reject_video_without_track(self) -> None:
        if self.preview_type is not PreviewType.VIDEO:
            return
        self._clear_playback_error()
        self.media_error_label.setText(
            "This file has no video track — only audio can be previewed."
        )
        self.media_error_label.show()

    def _on_media_status_changed(self, status) -> None:
        if (
            status == QMediaPlayer.MediaStatus.InvalidMedia
            and self.preview_type is PreviewType.VIDEO
            and not self.player.hasVideo()
        ):
            self._reject_video_without_track()

    def _play_pressed(self) -> None:
        if self.player is not None:
            self._clear_playback_error()
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
        self.media_error_label.setText(
            f"Playback unavailable: {error_string or str(error)}"
        )
        self.media_error_label.show()

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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._autoplay and self.player is not None:
            self._autoplay = False
            self._clear_playback_error()
            if self.preview_type is PreviewType.VIDEO:
                self._begin_playback()
            else:
                self.player.play()

    def closeEvent(self, event) -> None:
        if self.player is not None:
            self.player.stop()
        self._cancel_proxy()
        surface = getattr(self, "video_surface", None)
        if surface is not None:
            surface.clear_frame()
        super().closeEvent(event)


def _fmt_time(ms: int) -> str:
    total_seconds = max(0, ms) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"