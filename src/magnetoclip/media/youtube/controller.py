"""Playlist extraction controller (``media/youtube/controller.py``).

Owns a single background extraction at a time and translates the worker's
signals into application-level signals the UI can bind to. It does **not**
touch widgets; the dialog reads :attr:`probe` after :attr:`extract_finished`
fires.

Download *enqueueing* is deliberately left to the caller (see the manager
integration in ``core/downloads/manager.py``): this module only ever *resolves*
a playlist so the user can preview it.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from .types import YouTubePlaylist
from .worker import PlaylistExtractionWorker


class PlaylistController(QObject):
    """Runs one ``PlaylistExtractionWorker`` on a worker thread.

    Signals:
        extract_started() — extraction began.
        extract_finished(playlist) — resolve succeeded.
        extract_failed(message) — resolve failed (user-facing text).
        extract_cancelled() — resolve was aborted.
    """

    extract_started = Signal()
    extract_finished = Signal(object)
    extract_failed = Signal(str)
    extract_cancelled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: PlaylistExtractionWorker | None = None
        self.probe: YouTubePlaylist | None = None

    # ── public API ────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def extract(
        self,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bool:
        """Start resolving *url* in the background.

        Returns ``False`` (and does nothing) when another extraction is already
        running or *url* is empty, ``True`` otherwise. Results arrive through
        :attr:`extract_finished` / :attr:`extract_failed` / :attr:`extract_cancelled`.
        """
        if self.is_running:
            return False
        if not url:
            return False

        thread = QThread(self)
        worker = PlaylistExtractionWorker(engine_factory=self._make_engine)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.started.connect(self._on_started)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        self.probe = None
        worker.queue(url, cookies=cookies, headers=headers)
        thread.start()
        return True

    def cancel(self) -> None:
        """Request the running extraction to stop (no-op if idle)."""
        if self._worker is not None:
            self._worker.cancel()

    def shutdown(self) -> None:
        """Cancel and join the worker thread; safe to call on teardown."""
        self.cancel()
        self._finish_thread()

    # ── internals ─────────────────────────────────────────────────────────

    def _make_engine(self):
        from .engine import YouTubePlaylistEngine

        return YouTubePlaylistEngine()

    def _finish_thread(self) -> None:
        """Quit, join and drop the current worker/thread pair.

        Called from the GUI thread (via a queued slot after the worker emits
        its terminal signal), so by the time ``extract_finished`` /
        ``extract_failed`` / ``extract_cancelled`` reach the UI the thread is
        guaranteed to be joined — no half-dead threads on teardown.
        """
        thread = self._thread
        self._thread = None
        self._worker = None
        if thread is None:
            return
        if thread.isRunning():
            thread.quit()
            thread.wait(3000)
        thread.deleteLater()

    def _on_started(self) -> None:
        self.extract_started.emit()

    def _on_finished(self, playlist: YouTubePlaylist) -> None:
        self.probe = playlist
        self._finish_thread()
        self.extract_finished.emit(playlist)

    def _on_failed(self, message: str) -> None:
        self._finish_thread()
        self.extract_failed.emit(message)

    def _on_cancelled(self) -> None:
        self._finish_thread()
        self.extract_cancelled.emit()


__all__ = ["PlaylistController"]