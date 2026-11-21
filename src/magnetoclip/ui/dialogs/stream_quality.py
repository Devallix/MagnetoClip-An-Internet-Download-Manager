"""Fast, fail-safe resolution picker for HLS/DASH and streaming-site captures."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...media.stream_quality import (
    StreamQualityOption,
    StreamQualityProbe,
    best_only_probe,
    build_quality_options,
)
from ..components.buttons import AccentButton

log = logging.getLogger(__name__)


class _ProbeSignals(QObject):
    finished = Signal(object)


class _ProbeTask(QRunnable):
    """Resolve the available variants off the UI thread."""

    def __init__(
        self,
        url: str,
        kind: str,
        *,
        session_factory=None,
        request_context=None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        ffmpeg_available: bool = True,
    ) -> None:
        super().__init__()
        self.url = url
        self.kind = kind
        self.session_factory = session_factory
        self.request_context = request_context
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.ffmpeg_available = ffmpeg_available
        self.signals = _ProbeSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            result = self._probe()
        except Exception as exc:  # noqa: BLE001 - keep the download action usable
            log.exception("stream_quality_probe_unexpected_failure")
            result = best_only_probe(
                note="Available resolutions could not be read. Best available can still be downloaded.",
                source="fallback",
            )
        self.signals.finished.emit(result)

    def _probe(self) -> StreamQualityProbe:
        title = "Streaming video"
        formats: list[Any] = []
        source = "yt-dlp"
        errors: list[str] = []

        # HLS master playlists expose their actual height ladder directly.
        # Fetch through the browser with the captured frame/headers/cookies so
        # opaque playlist.php endpoints work just like conventional .m3u8 URLs.
        if self.kind == "hls" and self.session_factory is not None:
            try:
                from ...media.hls._browser_fetch import browser_fetch_text
                from ...media.hls.analyzer import analyze_master, title_from_page

                title = title_from_page(
                    getattr(self.request_context, "page_url", None), title
                )
                master_text = browser_fetch_text(
                    self.session_factory,
                    self.url,
                    context=self.request_context,
                    referer=getattr(self.request_context, "referer", None),
                    timeout=8.0,
                )
                media_info = analyze_master(
                    self.url,
                    master_text,
                    fetch_playlist=None,
                    title=title,
                )
                formats = list(media_info.formats or [])
                source = "HLS manifest"
                if formats:
                    options = build_quality_options(
                        formats,
                        ffmpeg_available=self.ffmpeg_available,
                        manifest_kind="hls",
                    )
                    if len(options) > 1 or (
                        media_info.formats
                        and not any(getattr(item, "height", None) for item in media_info.formats)
                    ):
                        note = ""
                        if len(options) == 1:
                            note = "This is a single-quality playlist. Best available is ready to download."
                        return StreamQualityProbe(
                            title=media_info.title or title,
                            options=options,
                            note=note,
                            source=source,
                        )
            except Exception as exc:  # noqa: BLE001 - yt-dlp remains a fallback
                errors.append(f"HLS probe: {exc}")
                log.debug("stream_quality_hls_probe_failed: %s", exc)

        # DASH, HLS fallback, and Generic extractor URLs are inspected with
        # yt-dlp. This does not request browser-profile cookies; only captured
        # request context is supplied to the resolver.
        try:
            from ...media.streaming import resolve_stream

            info = resolve_stream(
                self.url,
                "best",
                timeout=12.0,
                cookies=self.cookies or None,
                headers=self.headers or None,
                manifest_kind=self.kind or None,
            )
            title = info.title or title
            formats = list(info.formats or [])
            options = build_quality_options(
                formats,
                ffmpeg_available=self.ffmpeg_available,
                manifest_kind=self.kind,
            )
            if len(options) > 1:
                return StreamQualityProbe(
                    title=title,
                    options=options,
                    source="yt-dlp",
                )
            if formats:
                return StreamQualityProbe(
                    title=title,
                    options=options,
                    note="This stream exposes no separate resolution ladder. Best available is ready to download.",
                    source="yt-dlp",
                )
        except Exception as exc:  # noqa: BLE001 - never block the download
            errors.append(f"yt-dlp probe: {exc}")
            log.debug("stream_quality_ytdlp_probe_failed: %s", exc)

        # Critical fallback: keep a usable Best option even if metadata cannot
        # be read because the CDN blocks probes or the player URL is ephemeral.
        return best_only_probe(
            title=title,
            note="Available resolutions could not be read. You can still download using Best available; the downloader will use the best compatible source format.",
            source="fallback",
        )


class _QualityCard(QPushButton):
    def __init__(self, option: StreamQualityOption) -> None:
        super().__init__()
        self.option = option
        self.setObjectName("quality_card")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(58)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 9, 14, 9)
        row.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(2)
        title = QLabel(option.label)
        title.setObjectName("quality_resolution_best" if option.quality == "best" else "quality_resolution")
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        left.addWidget(title)
        detail = QLabel(option.detail or "Available source resolution")
        detail.setObjectName("quality_format")
        detail.setWordWrap(True)
        detail.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        left.addWidget(detail)
        row.addLayout(left, 1)
        if option.requires_merge:
            merge = QLabel("Merge audio")
            merge.setObjectName("quality_size")
            merge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            row.addWidget(merge)


class StreamingQualityDialog(QDialog):
    """Resolution picker whose Best option remains usable during/after probing."""

    def __init__(
        self,
        url: str,
        *,
        manifest_kind: str = "hls",
        context=None,
        request_context=None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        ffmpeg_available: bool | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self.manifest_kind = str(manifest_kind or "").lower()
        self.context = context
        self.request_context = request_context
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._selected_quality = "best"
        self._options: tuple[StreamQualityOption, ...] = build_quality_options([])
        self._cards: list[_QualityCard] = []
        self._task: _ProbeTask | None = None

        if ffmpeg_available is None:
            from ...media.streaming import _ffmpeg_available

            manager = getattr(context, "manager", None)
            ffmpeg_available = _ffmpeg_available(
                settings=getattr(manager, "settings", None)
            )
        self.ffmpeg_available = bool(ffmpeg_available)

        self.setWindowTitle("Choose streaming resolution")
        self.setMinimumSize(490, 340)
        self.resize(540, 470)
        self._build_ui()
        self._render_options(self._options)
        self._set_status("Checking available resolutions. Best available can be downloaded immediately.")
        self.download_button.setEnabled(True)
        self._start_probe()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QFrame()
        header.setObjectName("quality_header")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 13, 16, 13)
        header_layout.setSpacing(4)
        self.title_label = QLabel("Choose streaming resolution")
        self.title_label.setObjectName("page_title")
        header_layout.addWidget(self.title_label)
        self.subtitle_label = QLabel("Select the video quality to download.")
        self.subtitle_label.setObjectName("muted")
        self.subtitle_label.setWordWrap(True)
        header_layout.addWidget(self.subtitle_label)
        layout.addWidget(header)

        self.status_label = QLabel("")
        self.status_label.setObjectName("quality_status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("quality_scroll")
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.options_layout = QVBoxLayout(self.container)
        self.options_layout.setContentsMargins(0, 0, 4, 0)
        self.options_layout.setSpacing(8)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        self.note_label.setObjectName("muted")
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        self.download_button = AccentButton("Download")
        self.download_button.setEnabled(True)
        self.download_button.clicked.connect(self._accept_selection)
        buttons.addWidget(self.download_button)
        layout.addLayout(buttons)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("kind", "error" if error else "")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _render_options(self, options: tuple[StreamQualityOption, ...]) -> None:
        selected = self._selected_quality
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards.clear()
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._options = options or build_quality_options([])
        selected_index = 0
        for index, option in enumerate(self._options):
            card = _QualityCard(option)
            self._group.addButton(card, index)
            card.setChecked(option.quality == selected)
            if option.quality == selected:
                selected_index = index
            card.clicked.connect(self._on_card_clicked)
            self.options_layout.addWidget(card)
            self._cards.append(card)
        if self._cards and self._group.checkedId() < 0:
            self._cards[selected_index].setChecked(True)
        self.options_layout.addStretch(1)

    def _start_probe(self) -> None:
        session_factory = getattr(self.context, "session_factory", None)
        task = _ProbeTask(
            self.url,
            self.manifest_kind,
            session_factory=session_factory,
            request_context=self.request_context,
            cookies=self.cookies,
            headers=self.headers,
            ffmpeg_available=self.ffmpeg_available,
        )
        task.signals.finished.connect(self._on_probe_finished)
        self._task = task
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_probe_finished(self, probe: StreamQualityProbe) -> None:
        current = self._group.checkedId()
        if current >= 0 and current < len(self._options):
            self._selected_quality = self._options[current].quality
        self.title_label.setText(probe.title or "Streaming video")
        self._render_options(probe.options)
        if probe.note:
            self.note_label.setText(probe.note)
            self.note_label.setVisible(True)
            self._set_status("Resolution scan finished; downloading remains available.", error=False)
        else:
            self.note_label.setVisible(False)
            self._set_status("Choose a resolution, then click Download.")
        self.download_button.setEnabled(True)

    def _on_card_clicked(self) -> None:
        index = self._group.checkedId()
        if 0 <= index < len(self._options):
            self._selected_quality = self._options[index].quality

    def _accept_selection(self) -> None:
        index = self._group.checkedId()
        if 0 <= index < len(self._options):
            self._selected_quality = self._options[index].quality
        self.accept()

    @property
    def selected_quality(self) -> str:
        return self._selected_quality or "best"

    def reject(self) -> None:
        # Probe tasks have bounded network/yt-dlp timeouts and may safely finish
        # in the global pool after the dialog closes.
        super().reject()


__all__ = ["StreamingQualityDialog"]
