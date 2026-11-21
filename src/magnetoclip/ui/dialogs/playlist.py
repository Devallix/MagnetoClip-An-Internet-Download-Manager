"""Playlist preview dialog (``ui/dialogs/playlist.py``).

Shows a playlist resolved by :class:`PlaylistExtractionWorker` (through the
:class:`PlaylistController`), lets the user tick which videos to download, and
returns the selection. This dialog *never downloads* — the caller turns the
selected entries into per-video jobs through the existing manager.

Extraction state machine:
  extracting → ready (list) | error (retry) | cancelled (dialog closes)
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ...media.youtube.controller import PlaylistController
from ...media.youtube.types import PlaylistItemStatus, PlaylistVideo, YouTubePlaylist
from ..components.buttons import AccentButton

# Quality ladder offered for playlist downloads. ``ask`` is resolved by the
# caller (the per-video quality dialog flow); here it simply means "use the
# global default".
_PLAYLIST_QUALITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Best Available", "best"),
    ("2160p (4K)", "2160"),
    ("1440p", "1440"),
    ("1080p", "1080"),
    ("720p", "720"),
    ("480p", "480"),
    ("360p", "360"),
)


def format_seconds(seconds) -> str:
    """Format a duration in seconds as ``m:ss`` or ``h:mm:ss``."""
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _total_duration(playlist: YouTubePlaylist) -> float:
    return sum(
        (v.duration or 0.0) for v in playlist.videos if v.duration is not None
    )


# Compact while scanning; taller once the playlist is loaded so more rows fit.
_SCANNING_HEIGHT = 240
_READY_HEIGHT = 520


class PlaylistDialog(QDialog):
    """Preview a playlist and return the selected videos.

    Args:
        url: the playlist URL (``youtube.com/playlist?list=…`` or a
            ``watch?v=…&list=…`` URL).
        controller_factory: test hook — must return a
            :class:`PlaylistController`-like object.
        skipped_ids: YouTube video ids already in the download archive; the
            corresponding rows are shown checked-off as "already downloaded".
    """

    def __init__(
        self,
        url: str,
        *,
        context=None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        skipped_ids: set[str] | None = None,
        controller_factory=None,
        default_quality: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.url = url
        self.cookies = cookies
        self.headers = headers
        self.skipped_ids = set(skipped_ids or ())
        self.default_quality = default_quality or "best"
        self.playlist: YouTubePlaylist | None = None

        self._controller_factory = controller_factory or PlaylistController
        self.controller = self._controller_factory(self)

        self.setWindowTitle("Playlist")
        self.setMinimumSize(560, _SCANNING_HEIGHT)
        self._build_ui()
        self._show_extracting()
        self._update_button()

        self.controller.extract_finished.connect(self._on_extracted)
        self.controller.extract_failed.connect(self._on_failed)
        self.controller.extract_cancelled.connect(self.reject)
        self.controller.extract(url, cookies=cookies, headers=headers)

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.title_label = QLabel("Playlist")
        self.title_label.setObjectName("page_title")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("muted")
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.subtitle_label)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)

        self.select_all_check = QCheckBox("Select all")
        self.select_all_check.setVisible(False)
        self.select_all_check.toggled.connect(self._on_select_all_toggled)
        layout.addWidget(self.select_all_check)

        self.list_widget = QListWidget()
        self.list_widget.setVisible(False)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.list_widget, 1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        self.retry_button = QPushButton("Try again")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(self._retry)

        self.download_button = AccentButton("Download selected (0)")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.retry_button)
        buttons.addStretch(1)

        self.quality_combo = QComboBox()
        self.quality_combo.setFixedWidth(118)
        for label, value in _PLAYLIST_QUALITY_OPTIONS:
            self.quality_combo.addItem(label, value)
        default = self.default_quality if self.default_quality != "ask" else "best"
        quality_default = self.quality_combo.findData(default)
        self.quality_combo.setCurrentIndex(max(0, quality_default))
        buttons.addWidget(self.quality_combo)

        self.download_button = AccentButton("Download selected (0)")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.accept)
        buttons.addWidget(self.download_button)
        layout.addLayout(buttons)

    # ── state transitions ────────────────────────────────────────────────

    def _resize_to(self, height: int) -> None:
        """Shrink/grow the dialog for the current state (scanning vs ready)."""
        self.setMinimumHeight(height)
        self.resize(max(self.width(), self.minimumWidth()), height)

    def _show_extracting(self) -> None:
        self._resize_to(_SCANNING_HEIGHT)
        self.title_label.setText("Extracting playlist\u2026")
        self.subtitle_label.setText("")
        self.status_label.setText("")
        self.status_label.setStyleSheet("")
        self.progress.setVisible(True)
        self.select_all_check.setVisible(False)
        self.select_all_check.setChecked(False)
        self.list_widget.setVisible(False)
        self.list_widget.clear()
        self.retry_button.setVisible(False)
        self.download_button.setEnabled(False)

    def _show_error(self, message: str) -> None:
        self._resize_to(_SCANNING_HEIGHT)
        self.title_label.setText("Could not open this playlist")
        self.subtitle_label.setText("")
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #d44;")
        self.progress.setVisible(False)
        self.select_all_check.setVisible(False)
        self.list_widget.setVisible(False)
        self.list_widget.clear()
        self.retry_button.setVisible(True)
        self.download_button.setEnabled(False)

    def _show_ready(self, playlist: YouTubePlaylist) -> None:
        self._resize_to(_READY_HEIGHT)
        self.playlist = playlist
        self.title_label.setText(playlist.title or "Untitled playlist")
        duration = format_seconds(_total_duration(playlist))
        total = f"{playlist.video_count} videos"
        if duration:
            total += f" \u2022 {duration}"
        self.subtitle_label.setText(total)
        self.status_label.setText("")
        self.status_label.setStyleSheet("")

        self.select_all_check.setVisible(True)
        self.select_all_check.blockSignals(True)
        self.select_all_check.setChecked(True)
        self.select_all_check.blockSignals(False)

        skip_note = (
            f" ({len(self.skipped_ids)} already downloaded)"
            if self.skipped_ids
            else ""
        )
        self.progress.setVisible(False)
        self.list_widget.setVisible(True)
        self.list_widget.blockSignals(True)
        try:
            self.list_widget.clear()
            for video in playlist.videos:
                skippable = video.video_id in self.skipped_ids
                suffix = " \u2022 already downloaded" if skippable else ""
                text = f"{video.index:02d} \u2022 {video.title}{suffix}"
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, video.index)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.Unchecked if skippable else Qt.Checked
                )
                if skippable:
                    item.setForeground(_muted_brush())
                self.list_widget.addItem(item)
            self.subtitle_label.setText(
                f"{playlist.video_count} videos \u2022 "
                f"{duration}{skip_note}".strip(" \u2022")
            )
        finally:
            self.list_widget.blockSignals(False)
        self._update_button()

    # ── signals ──────────────────────────────────────────────────────────

    def _on_extracted(self, playlist: YouTubePlaylist) -> None:
        self._show_ready(playlist)

    def _on_failed(self, message: str) -> None:
        self._show_error(message)

    def _on_select_all_toggled(self, checked: bool) -> None:
        if self.playlist is None:
            return
        state = Qt.Checked if checked else Qt.Unchecked
        self.list_widget.blockSignals(True)
        try:
            for row in range(self.list_widget.count()):
                item = self.list_widget.item(row)
                if item.data(Qt.UserRole) is not None:
                    item.setCheckState(state)
        finally:
            self.list_widget.blockSignals(False)
        self._update_button()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        self._update_button()

    def _retry(self) -> None:
        self._show_extracting()
        self.controller.extract(
            self.url, cookies=self.cookies, headers=self.headers
        )

    # ── results ──────────────────────────────────────────────────────────

    def _update_button(self) -> None:
        count = len(self.selected_videos())
        self.download_button.setText(f"Download selected ({count})")
        self.download_button.setEnabled(
            self.playlist is not None and count > 0
        )

    def selected_videos(self) -> list[PlaylistVideo]:
        if self.playlist is None:
            return []
        by_index = {video.index: video for video in self.playlist.videos}
        selected: list[PlaylistVideo] = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            index = item.data(Qt.UserRole)
            if index is None:
                continue
            if item.checkState() == Qt.Checked:
                video = by_index.get(index)
                if video is not None:
                    video.status = PlaylistItemStatus.selected
                    selected.append(video)
        return selected

    def selected_quality(self) -> str | None:
        """The quality key (``"best"``/height) chosen by the combo, or None."""
        data = self.quality_combo.currentData()
        return data if data else None

    def _skipped_count(self) -> int:
        return len(self.skipped_ids)

    def reject(self) -> None:
        if self.controller.is_running:
            self.controller.cancel()
        super().reject()

    def closeEvent(self, event) -> None:
        if self.controller.is_running:
            self.controller.cancel()
        self.controller.shutdown()
        super().closeEvent(event)


def _muted_brush():
    from PySide6.QtGui import QBrush, QColor

    return QBrush(QColor(128, 128, 128))


__all__ = ["PlaylistDialog", "format_seconds"]