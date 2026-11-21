"""Resolution picker for browser-captured HLS/M3U8 streams.

The master playlist is resolved on a worker thread through the browser's
request context, then the user selects the actual advertised video variant.
The selected height is persisted with that download so the downloader cannot
silently fall back to the global quality setting.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


_ACTIVE_WORKERS: set[QThread] = set()


class _HlsResolveWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, session_factory, url: str, request_context, parent=None) -> None:
        super().__init__(parent)
        self.session_factory = session_factory
        self.url = url
        self.request_context = request_context

    def run(self) -> None:  # noqa: D401 - Qt worker entry point
        try:
            from ...media.hls.analyzer import resolve_hls_media

            info = resolve_hls_media(
                self.url,
                session_factory=self.session_factory,
                context=self.request_context,
                title="stream",
            )
            formats = [item for item in (info.formats or []) if item.has_video and not item.audio_only]
            # Collapse bitrate duplicates at the same vertical resolution,
            # retaining the highest-bandwidth presentation for that height.
            by_height: dict[int | None, object] = {}
            for item in formats:
                key = item.height
                current = by_height.get(key)
                if current is None or int(item.bandwidth or 0) > int(current.bandwidth or 0):
                    by_height[key] = item
            ordered = sorted(
                by_height.values(),
                key=lambda item: (item.height or 0, item.bandwidth or 0),
                reverse=True,
            )
            if not ordered:
                raise RuntimeError("The playlist did not advertise any downloadable video resolutions.")
            self.ready.emit(ordered)
        except Exception as exc:  # noqa: BLE001 - return a useful dialog error
            self.failed.emit(str(exc) or "Could not read the HLS resolution list.")


class HlsQualityDialog(QDialog):
    """Resolve an HLS master and let the user choose an advertised resolution."""

    def __init__(
        self,
        url: str,
        *,
        session_factory,
        request_context=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self.selected_quality: str | None = None
        self._formats: list[object] = []
        self._worker = _HlsResolveWorker(
            session_factory, url, request_context, parent=None
        )
        _ACTIVE_WORKERS.add(self._worker)
        self._worker.finished.connect(lambda worker=self._worker: _ACTIVE_WORKERS.discard(worker))
        self._worker.ready.connect(self._on_ready)
        self._worker.failed.connect(self._on_failed)

        self.setWindowTitle("Choose video resolution")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        heading = QLabel("Choose video resolution")
        heading.setObjectName("page_title")
        layout.addWidget(heading)

        self.status = QLabel("Reading available resolutions from the stream…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.choices = QComboBox()
        self.choices.setEnabled(False)
        layout.addWidget(self.choices)

        self.audio_note = QLabel(
            "MagnetoClip will include the stream's audio rendition when the playlist provides one."
        )
        self.audio_note.setWordWrap(True)
        layout.addWidget(self.audio_note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.download_button = QPushButton("Download")
        self.download_button.setDefault(True)
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._accept_choice)
        buttons.addWidget(self.download_button)
        layout.addLayout(buttons)

        self._worker.start()

    def _on_ready(self, formats) -> None:
        self._formats = list(formats or [])
        self.choices.clear()
        for item in self._formats:
            height = getattr(item, "height", None)
            quality = str(height) if height else "best"
            label = f"{height}p" if height else "Available stream"
            bandwidth = int(getattr(item, "bandwidth", 0) or 0)
            if bandwidth:
                label += f"  ·  {bandwidth / 1_000_000:.2f} Mbps"
            self.choices.addItem(label, quality)
        self.choices.setEnabled(bool(self._formats))
        self.download_button.setEnabled(bool(self._formats))
        self.status.setText(
            f"Found {len(self._formats)} available video option(s). Select the resolution to download."
        )

    def _on_failed(self, message: str) -> None:
        self.status.setText("Could not read the resolution list: " + message)
        self.choices.setEnabled(False)
        self.download_button.setEnabled(False)

    def _accept_choice(self) -> None:
        if self.choices.currentIndex() < 0:
            return
        self.selected_quality = str(self.choices.currentData() or "best")
        self.accept()

    def reject(self) -> None:
        # This worker owns no GUI objects and remains in the module-level set
        # until its browser fetch completes, so closing the dialog cannot
        # destroy a live QThread.
        super().reject()
