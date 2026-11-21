"""YouTube quality-selection dialog (``ui/dialogs/youtube_quality.py``).

Shows the *real* resolution ladder (extracted by
:class:`YouTubeMetadataController` on a background thread), lets the user pick
one, and exposes the resulting :class:`SelectedYouTubeQuality` so the caller
can enqueue the download with a per-download ``-f`` selector. This dialog
never downloads; it only resolves metadata and returns a choice.

State machine (mirrors the playlist dialog):
   extracting → ready (options) | error (retry) | cancelled (dialog closes)

When ffmpeg is missing, merged (video-only + audio-only) options are hidden
and a note explains why. The caller wires ``on_ffmpeg_located`` if it wants a
"Locate FFmpeg" escape hatch.

Options are rendered as selectable cards (resolution, format, estimated size)
rather than a plain list, so the ladder reads at a glance.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...media.youtube.formats import (
    QualityOption,
    SelectedYouTubeQuality,
    YouTubeFormatAnalyzer,
)
from ...media.youtube.metadata_worker import (
    VideoMetadataResult,
    YouTubeMetadataController,
)
from ..components.buttons import AccentButton
from ..components.liquid_progress import LiquidProgressBar
from ..util import format_bytes

from .playlist import format_seconds

# Compact while scanning; taller once the ladder is loaded.
_SCANNING_HEIGHT = 240
_READY_HEIGHT = 560
_CARD_HEIGHT = 56


class _QualityCard(QPushButton):
    """One selectable resolution row: label, format line and estimated size."""

    def __init__(self, option: QualityOption) -> None:
        super().__init__()
        self.option = option
        self.setObjectName("quality_card")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(_CARD_HEIGHT)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)

        resolution = QLabel(option.label)
        resolution.setObjectName(
            "quality_resolution_best"
            if option.requested_height is None
            else "quality_resolution"
        )
        row.addWidget(resolution)

        if option.format_label:
            fmt = QLabel(option.format_label)
            fmt.setObjectName("quality_format")
            if option.requires_merge:
                fmt.setText(f"{option.format_label} \u2022 merge")
            row.addWidget(fmt)

        row.addStretch(1)

        size = option.estimated_size
        if size:
            size_text = f"{'~' if option.size_estimated else ''}{format_bytes(size)}"
        else:
            size_text = "size unknown"
        size_label = QLabel(size_text)
        size_label.setObjectName("quality_size")
        row.addWidget(size_label)


class YouTubeQualityDialog(QDialog):
    """Resolve a YouTube video's formats and return the picked quality.

    Args:
        url: the watch/video URL.
        context: app context (unused directly, kept for API symmetry).
        cookies/headers: stored browser capture data passed to extraction.
        default_quality: ``"ask"`` or a height (e.g. ``"720"``); used only to
            pre-select the matching option once the ladder is known.
        controller_factory: test hook returning a controller-like object.
        ffmpeg_available: ``None`` probes at construction; when ``False`` the
            merged-only resolutions are hidden.
        on_ffmpeg_located: optional callback(str path) invoked after the user
            picks an ffmpeg binary; the dialog re-extracts afterwards.
        max_options: cap the number of ladder rows (test hook).
    """

    def __init__(
        self,
        url: str,
        *,
        context=None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        default_quality: str | None = None,
        controller_factory=None,
        ffmpeg_available: bool | None = None,
        on_ffmpeg_located=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.url = url
        self.cookies = cookies
        self.headers = headers
        self.default_quality = default_quality or "ask"
        self.result: SelectedYouTubeQuality | None = None
        self.probe: VideoMetadataResult | None = None
        self._on_ffmpeg_located = on_ffmpeg_located

        if ffmpeg_available is None:
            from ...media.streaming import _ffmpeg_available

            manager = getattr(context, "manager", None)
            settings = getattr(manager, "settings", None)
            ffmpeg_available = _ffmpeg_available(settings=settings)
        self.ffmpeg_available = ffmpeg_available

        self._controller_factory = controller_factory or YouTubeMetadataController
        self.controller = self._controller_factory(self)

        self.setWindowTitle("Download quality")
        self.setMinimumSize(520, _SCANNING_HEIGHT)
        self._build_ui()
        self._show_extracting()
        self._update_button()

        self.controller.metadata_finished.connect(self._on_ready)
        self.controller.metadata_failed.connect(self._on_failed)
        self.controller.extract(
            url,
            cookies=cookies,
            headers=headers,
            ffmpeg_available=self.ffmpeg_available,
        )

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QFrame()
        header.setObjectName("quality_header")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(4)

        self.title_label = QLabel("Download quality")
        self.title_label.setObjectName("page_title")
        self.title_label.setWordWrap(True)
        header_layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("muted")
        self.subtitle_label.setWordWrap(True)
        header_layout.addWidget(self.subtitle_label)
        layout.addWidget(header)

        self.status_label = QLabel("")
        self.status_label.setObjectName("quality_status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = LiquidProgressBar()
        self.progress.set_indeterminate(True)
        self.progress.setFixedHeight(10)
        layout.addWidget(self.progress)

        self.options_scroll = QScrollArea()
        self.options_scroll.setObjectName("quality_scroll")
        self.options_scroll.setWidgetResizable(True)
        self.options_container = QWidget()
        self.options_container.setObjectName("quality_options")
        self.options_layout = QVBoxLayout(self.options_container)
        self.options_layout.setContentsMargins(0, 0, 4, 0)
        self.options_layout.setSpacing(8)
        self.options_scroll.setWidget(self.options_container)
        self.options_scroll.setVisible(False)
        layout.addWidget(self.options_scroll, 1)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        self.ffmpeg_note = QLabel("")
        self.ffmpeg_note.setWordWrap(True)
        self.ffmpeg_note.setVisible(False)
        layout.addWidget(self.ffmpeg_note)

        self.locate_button = QPushButton("Locate ffmpeg\u2026")
        self.locate_button.setVisible(False)
        self.locate_button.clicked.connect(self._locate_ffmpeg)
        layout.addWidget(self.locate_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        self.retry_button = QPushButton("Try again")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(self._retry)

        self.download_button = AccentButton("Download")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._on_download_clicked)

        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.retry_button)
        buttons.addStretch(1)
        buttons.addWidget(self.download_button)
        layout.addLayout(buttons)

    # ── state transitions ────────────────────────────────────────────────

    def _resize_to(self, height: int) -> None:
        self.setMinimumHeight(height)
        self.resize(max(self.width(), self.minimumWidth()), height)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(message)
        self.status_label.setProperty("kind", "error" if error else "")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _clear_options(self) -> None:
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_extracting(self) -> None:
        self._resize_to(_SCANNING_HEIGHT)
        self.title_label.setText("Reading video info\u2026")
        self.subtitle_label.setText("")
        self._set_status("", error=False)
        self.progress.setVisible(True)
        self.options_scroll.setVisible(False)
        self._clear_options()
        self.retry_button.setVisible(False)
        self.ffmpeg_note.setVisible(False)
        self.locate_button.setVisible(False)
        self.download_button.setEnabled(False)

    def _show_error(self, message: str) -> None:
        self._resize_to(_SCANNING_HEIGHT)
        self.title_label.setText("Could not read this video")
        self.subtitle_label.setText("")
        self._set_status(message, error=True)
        self.progress.setVisible(False)
        self.options_scroll.setVisible(False)
        self._clear_options()
        self.retry_button.setVisible(True)
        self.ffmpeg_note.setVisible(False)
        self.locate_button.setVisible(False)
        self.download_button.setEnabled(False)

    def _show_ready(self, probe: VideoMetadataResult) -> None:
        self._resize_to(_READY_HEIGHT)
        self.probe = probe
        self.title_label.setText(probe.title or "YouTube video")

        duration = format_seconds(probe.duration)
        subtitle_bits = [probe.ext.upper()]
        if probe.best_height:
            subtitle_bits.append(f"up to {_height_label(probe.best_height)}")
        if duration:
            subtitle_bits.append(duration)
        self.subtitle_label.setText(" \u2022 ".join(subtitle_bits))
        self._set_status("", error=False)

        options = probe.options
        analyzer = YouTubeFormatAnalyzer(
            probe.formats, duration=probe.duration
        )
        default_on = self._default_index(options, analyzer)
        self._clear_options()
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        for index, option in enumerate(options):
            card = _QualityCard(option)
            self._button_group.addButton(card, index)
            card.setChecked(index == default_on)
            card.clicked.connect(self._on_card_clicked)
            self.options_layout.addWidget(card)
        self.options_scroll.setVisible(True)

        if not self.ffmpeg_available:
            self.ffmpeg_note.setVisible(True)
            self.ffmpeg_note.setText(
                "ffmpeg not found \u2014 formats that need audio/video merging "
                "are hidden. Only the single-file resolutions below are offered."
            )
            self.locate_button.setVisible(self._on_ffmpeg_located is not None)
        self._update_button()

    def _default_index(
        self, options: list[QualityOption], analyzer: dict
    ) -> int:
        if not options:
            return -1
        requested = (self.default_quality or "ask").lower()
        if requested in ("", "ask"):
            return 0
        if requested == "best":
            return 0
        try:
            height = int(requested)
        except (TypeError, ValueError):
            return 0
        for index, option in enumerate(options):
            if option.requested_height == height:
                return index
        # Requested height unavailable: nearest below (lower-if-unavailable).
        lowers = [
            (index, option)
            for index, option in enumerate(options[1:], start=1)
            if option.requested_height and option.requested_height < height
        ]
        if lowers:
            return max(lowers, key=lambda item: item[1].requested_height)[0]
        return 0

    # ── signals ──────────────────────────────────────────────────────────

    def _on_ready(self, probe: VideoMetadataResult) -> None:
        self._show_ready(probe)

    def _on_failed(self, message: str) -> None:
        self._show_error(message)

    def _on_card_clicked(self) -> None:
        self._update_button()

    def _retry(self) -> None:
        self._show_extracting()
        self.controller.extract(
            self.url,
            cookies=self.cookies,
            headers=self.headers,
            ffmpeg_available=self.ffmpeg_available,
        )

    # ── results ──────────────────────────────────────────────────────────

    def _update_button(self) -> None:
        self.download_button.setEnabled(
            self.probe is not None and self.selected_option() is not None
        )

    def selected_option(self) -> QualityOption | None:
        if self.probe is None:
            return None
        index = self._button_group.checkedId()
        if 0 <= index < len(self.probe.options):
            return self.probe.options[index]
        return None

    def selected_result(self) -> SelectedYouTubeQuality | None:
        option = self.selected_option()
        if self.probe is None or option is None:
            return None
        analyzer = YouTubeFormatAnalyzer(
            self.probe.formats, duration=self.probe.duration
        )
        return analyzer.select(
            option.requested_height, ffmpeg_available=self.ffmpeg_available
        )

    def _on_download_clicked(self) -> None:
        self.result = self.selected_result()
        self.accept()

    def _locate_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate ffmpeg", "", "ffmpeg (ffmpeg*.exe);;All files (*)"
        )
        if not path:
            return
        if self._on_ffmpeg_located is not None:
            self._on_ffmpeg_located(path)
        self.ffmpeg_available = True
        self._retry()

    def reject(self) -> None:
        if self.controller.is_running:
            self.controller.cancel()
        super().reject()

    def closeEvent(self, event) -> None:
        if self.controller.is_running:
            self.controller.cancel()
        self.controller.shutdown()
        super().closeEvent(event)


def _height_label(height: int | None) -> str:
    from ...media.youtube.formats import display_height

    return display_height(height)


__all__ = ["YouTubeQualityDialog"]