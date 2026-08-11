"""Bridges browser captures into the Qt UI.

The native-messaging host runs in a separate process and cannot show dialogs or
tray notifications, so it persists ``pending_captures`` and ``browser_detections``
rows. This watcher polls the shared database on the UI thread, presents a
confirmation dialog for each capture, and raises a tray notification when the
extension finds downloadable files on a page.
"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import QObject, QTimer

from magnetoclip.core.events.bus import Events
from magnetoclip.database.repositories import (
    BrowserDetectionRepository,
    PendingCaptureRepository,
)
from magnetoclip.services.logging import get_logger

from ..dialogs.capture import (
    RESULT_DOWNLOAD_NOW,
    RESULT_SKIP,
    RESULT_SKIP_ALL,
    CaptureDialog,
)

log = get_logger(__name__)

POLL_INTERVAL_MS = 1200


class CaptureWatcher(QObject):
    """Periodically checks for pending browser captures and page detections."""

    def __init__(self, context, parent=None, dialog_factory=None) -> None:
        super().__init__(parent)
        self.context = context
        self._dialog_factory = dialog_factory or self._default_dialog
        self._dialog_open = False
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.poll)

    def _default_dialog(self, capture) -> CaptureDialog:
        return CaptureDialog(
            self.context,
            url=capture.url,
            filename=capture.filename,
            detected_type=capture.detected_type,
            referrer=capture.referrer,
            parent=self.parent(),
        )

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def poll(self) -> int:
        """Process detections and captures. Returns the number of items handled."""
        handled = self._handle_detections()
        if not self._dialog_open:
            handled += self._handle_captures()
        return handled

    # ----- page detections (notifications) -----

    def _handle_detections(self) -> int:
        if not bool(self.context.settings.get("browser.notify_downloadable", True)):
            return 0
        with self.context.session_factory() as session:
            repo = BrowserDetectionRepository(session)
            detections = repo.unnotified()
            if not detections:
                return 0
            for detection in detections:
                repo.mark_notified(detection.id)
        for detection in detections:
            host = urlparse(detection.page_url).hostname or detection.page_url
            self.context.events.post(
                Events.NOTIFICATION_REQUESTED,
                {
                    "kind": "info",
                    "title": "Downloadable files detected",
                    "body": f"{detection.count} file(s) available on {host}",
                },
            )
        return len(detections)

    # ----- pending captures (confirmation dialog) -----

    def _handle_captures(self) -> int:
        with self.context.session_factory() as session:
            repo = PendingCaptureRepository(session)
            repo.expire_stale()
            pending = repo.pending()
        if not pending:
            return 0
        capture = pending[0]
        self._dialog_open = True
        try:
            dialog = self._dialog_factory(capture)
            result = dialog.exec()
        except Exception:  # noqa: BLE001 - a failing dialog must not crash the UI
            log.warning("capture_dialog_failed", capture_id=capture.id, exc_info=True)
            self._resolve(capture.id, "expired")
            return 0
        finally:
            self._dialog_open = False
        self._apply_decision(capture, result, dialog)
        return 1

    def _apply_decision(self, capture, result, dialog) -> None:
        if result == RESULT_SKIP_ALL:
            self._reject_all_pending()
            return
        if result == RESULT_SKIP:
            self._resolve(capture.id, "rejected")
            return
        try:
            download = self.context.manager.add(
                capture.url,
                filename=dialog.filename(),
                save_dir=dialog.directory(),
                category_name=dialog.category(),
                connections_max=dialog.connections(),
                headers={"Referer": capture.referrer} if capture.referrer else None,
                cookies=capture.cookies_json,
            )
        except Exception as exc:  # noqa: BLE001 - surface add failures as a rejection
            log.warning("capture_add_failed", capture_id=capture.id, error=str(exc))
            self._resolve(capture.id, "rejected")
            return
        self._resolve(capture.id, "approved", download_id=download.id)
        if result == RESULT_DOWNLOAD_NOW:
            self._start(download.id)

    def _start(self, download_id: int) -> None:
        try:
            self.context.manager.start(download_id)
        except RuntimeError:
            # No event loop running (headless test); the download stays queued.
            log.debug("capture_start_deferred", download_id=download_id)

    def _resolve(self, capture_id: int, status: str, download_id: int | None = None) -> None:
        with self.context.session_factory() as session:
            PendingCaptureRepository(session).resolve(
                capture_id, status, download_id=download_id
            )

    def _reject_all_pending(self) -> None:
        with self.context.session_factory() as session:
            PendingCaptureRepository(session).resolve_all("rejected")
