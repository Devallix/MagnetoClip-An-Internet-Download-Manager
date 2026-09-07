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

from magnetoclip.browser.skip import skip_all_active
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
        self._download_fingerprint: dict[int, tuple[str, ...]] = {}
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
        self._sync_downloads()
        if not self._dialog_open:
            handled += self._handle_captures()
        return handled

    def _sync_downloads(self) -> None:
        """Mirror download rows written by the browser-host process into the GUI.

        The native-messaging host runs in a separate process and only shares the
        database, so the archive downloads it creates never emit events here.
        Every poll we fingerprint the most recent downloads and re-emit local
        events for rows that changed, so the Downloads page reflects host-side
        archive jobs (progress -> completed) without waiting for a manual page
        refresh. GUI-initiated downloads are already live via normal events, so
        re-emitting for them is harmless deduplication.
        """
        from sqlalchemy import select

        from magnetoclip.database.models import Download

        with self.context.session_factory() as session:
            rows = session.execute(
                select(
                    Download.id,
                    Download.status,
                    Download.filename,
                    Download.detected_type,
                    Download.size_total,
                    Download.size_downloaded,
                    Download.save_path,
                    Download.completed_at,
                    Download.error,
                )
                .order_by(Download.created_at.desc())
                .limit(200)
            ).all()
        if not rows:
            self._download_fingerprint = {}
            return
        seen: dict[int, tuple[str, ...]] = {}
        for row in rows:
            seen[int(row.id)] = tuple(
                str(value) if value is not None else "" for value in row
            )
        if seen == self._download_fingerprint:
            return
        previous = self._download_fingerprint
        self._download_fingerprint = seen
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        for download_id, fingerprint in seen.items():
            if previous.get(download_id) == fingerprint:
                continue
            download = manager.get_download(download_id)
            if download is None:
                continue
            self.context.events.post(
                (
                    Events.DOWNLOAD_ADDED
                    if download_id not in previous
                    else Events.DOWNLOAD_UPDATED
                ),
                manager.snapshot_item(download),
            )

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
                    "action": "detected",
                },
            )
        return len(detections)

    # ----- pending captures (confirmation dialog) -----

    def _handle_captures(self) -> int:
        with self.context.session_factory() as session:
            repo = PendingCaptureRepository(session)
            repo.expire_stale()
            pending = repo.pending()
            if pending and skip_all_active(self.context):
                repo.resolve_all("rejected")
                pending = []
        if not pending:
            return 0
        capture = pending[0]
        log.info(
            "capture_dialog_showing",
            capture_id=capture.id,
            url=capture.url,
            filename=capture.filename,
        )
        self._dialog_open = True
        # Raise the main window before showing the dialog so it isn't lost
        # behind the browser when the user right-clicks to capture a file.
        parent = self.parent()
        if parent is not None:
            try:
                parent.show()
                parent.raise_()
                parent.activateWindow()
            except Exception:  # noqa: BLE001 - window activation is best-effort
                pass
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
            self._park_all_pending_as_detections()
            return
        if result == RESULT_SKIP:
            self._resolve(capture.id, "rejected")
            return
        try:
            data = self._decode_data(getattr(capture, "data_base64", None))
            download = self.context.manager.add(
                capture.url,
                filename=dialog.filename(),
                save_dir=dialog.directory(),
                category_name=dialog.category(),
                connections_max=dialog.connections(),
                headers={"Referer": capture.referrer} if capture.referrer else None,
                cookies=capture.cookies_json,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001 - surface add failures as a rejection
            log.warning("capture_add_failed", capture_id=capture.id, error=str(exc))
            self._resolve(capture.id, "rejected")
            return
        self._resolve(capture.id, "approved", download_id=download.id)
        if result == RESULT_DOWNLOAD_NOW and data is None:
            self._start(download.id)

    @staticmethod
    def _decode_data(data_base64: str | None) -> bytes | None:
        """Decode inline capture data; None when absent or corrupt."""
        if not data_base64:
            return None
        try:
            import base64

            return base64.b64decode(data_base64, validate=True)
        except Exception:  # noqa: BLE001 - corrupt data falls back to a normal add
            return None

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

    def _park_all_pending_as_detections(self) -> None:
        """Move every queued capture to the Detection page.

        "Skip all" does not arm the persistent skip flag anymore — arming it
        also ticked the "Skip all detected files without asking" checkbox in
        Settings, and silenced future captures the user did not ask to silence.
        It just parks the already-detected files as page detections so they can
        be reviewed or downloaded from the Detection page; future captures keep
        popping the confirmation dialog.
        """
        with self.context.session_factory() as session:
            repo = PendingCaptureRepository(session)
            by_page: dict[str, list[dict]] = {}
            for capture in repo.pending():
                page = capture.referrer or capture.url
                file: dict = {
                    "url": capture.url,
                    "filename": capture.filename or "",
                    "detected_type": capture.detected_type or "file",
                }
                if capture.data_base64:
                    file["data_base64"] = capture.data_base64
                by_page.setdefault(page, []).append(file)
            detection_repo = BrowserDetectionRepository(session)
            for page, files in by_page.items():
                detection_repo.add(
                    page, count=len(files), files=files[:50], notified=True
                )
            repo.resolve_all("rejected")
