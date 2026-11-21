"""Threaded playlist extraction worker (``media/youtube/worker.py``).

Extraction is blocking (yt-dlp page fetch + parse), so it runs on a
``QThread``. The worker is a plain ``QObject`` that **never touches the UI**:
everything it needs to communicate is a signal.

Typical usage (see :mod:`magnetoclip.media.youtube.controller`):

    thread = QThread()
    worker = PlaylistExtractionWorker()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.cancelled.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    worker.queue(url, cookies=cookies, headers=headers)
    thread.start()
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from .engine import PlaylistExtractionCancelled, PlaylistExtractionError, YouTubePlaylistEngine


class PlaylistExtractionWorker(QObject):
    """Runs a :class:`YouTubePlaylistEngine` extraction on a worker thread.

    Signals:
        started() — extraction began.
        finished(playlist) — :class:`YouTubePlaylist` ready for preview.
        failed(message) — extraction failed (user-facing text).
        cancelled() — extraction was aborted via :meth:`cancel`.
    """

    started = Signal()
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        engine_factory=None,
    ) -> None:
        super().__init__(parent)
        self._params: dict[str, Any] | None = None
        self._cancel_requested = False
        self._engine: YouTubePlaylistEngine | None = None
        self._engine_factory = engine_factory or (
            lambda: YouTubePlaylistEngine()
        )

    # ── public API (any thread) ───────────────────────────────────────────

    def queue(
        self,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store the extraction parameters; safe to call before the thread is
        started (i.e. from the GUI thread)."""
        self._params = {"url": url, "cookies": cookies, "headers": headers}

    @Slot()
    def cancel(self) -> None:
        """Ask the running extraction to stop (polled between entries)."""
        self._cancel_requested = True

    # ── worker-thread entry point ─────────────────────────────────────────

    @Slot()
    def run(self) -> None:
        params = self._params
        if not params:
            self.failed.emit("no extraction pending")
            return
        self.started.emit()
        engine = self._engine_instance()
        try:
            playlist = engine.extract(
                params["url"],
                cookies=params.get("cookies"),
                headers=params.get("headers"),
                cancelled=self._is_cancelled,
            )
        except PlaylistExtractionCancelled:
            self.cancelled.emit()
        except PlaylistExtractionError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.failed.emit(f"failed to extract playlist: {exc}")
        else:
            self.finished.emit(playlist)

    # ── internals ─────────────────────────────────────────────────────────

    def _is_cancelled(self) -> bool:
        return self._cancel_requested

    def _engine_instance(self) -> YouTubePlaylistEngine:
        if self._engine is None:
            self._engine = self._engine_factory()
        return self._engine


__all__ = ["PlaylistExtractionWorker"]