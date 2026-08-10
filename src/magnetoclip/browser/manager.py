"""Browser integration manager: routes native-messaging requests to the app."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from magnetoclip.core.events.bus import Events
from magnetoclip.database.models import BrowserEvent
from magnetoclip.database.repositories import (
    BrowserDetectionRepository,
    PendingCaptureRepository,
)
from magnetoclip.services.logging import get_logger

log = get_logger(__name__)


class BrowserManager:
    """Handles requests from the browser extension over native messaging."""

    def __init__(self, context, manager=None) -> None:
        self.context = context
        self.manager = manager or getattr(context, "manager", None)
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = message.get("id")
        if not isinstance(request_id, (int, str)):
            request_id = None
        response = self._route(message)
        if request_id is not None:
            response["id"] = request_id
        return response

    def _route(self, message: dict[str, Any]) -> dict[str, Any]:
        message_type = message.get("type")
        if message_type == "ping":
            return {"type": "pong"}
        if message_type == "status":
            return self._status()
        if message_type == "settings":
            return self._settings()
        if message_type == "capture":
            return self._capture(message)
        if message_type == "page_scan":
            return self._page_scan(message)
        if message_type == "capture_result":
            return self._capture_result(message)
        return {"type": "error", "message": f"unknown message type: {message_type}"}

    # ----- handlers -----

    def _capture(self, message: dict[str, Any]) -> dict[str, Any]:
        if not self.context.settings.get("browser.integration_enabled", False):
            return {
                "type": "capture_error",
                "message": "integration disabled in MagnetoClip",
            }
        url = str(message.get("url") or "").strip()
        error = self._validate_url(url)
        if error:
            return {"type": "capture_error", "message": error}
        filename = message.get("filename") or None
        self._record_event(message, url)

        # Auto-detected media from social platforms always goes through the
        # confirmation dialog; it must never silently download a whole feed.
        auto_detected = message.get("source") == "page_scan"
        if auto_detected or self.context.settings.get("browser.confirm_capture", True):
            capture = self._enqueue_pending_capture(message, url)
            self.context.events.post(
                Events.BROWSER_EVENT,
                {
                    "url": url,
                    "source": message.get("source"),
                    "pending_capture_id": capture.id,
                    "filename": capture.filename,
                },
            )
            return {
                "type": "capture_pending",
                "id": capture.id,
                "url": url,
                "filename": capture.filename or "",
            }

        if self.manager is None:
            return {"type": "capture_error", "message": "download manager unavailable"}
        return self._start_immediately(message, url, filename)

    def _start_immediately(
        self, message: dict[str, Any], url: str, filename: str | None
    ) -> dict[str, Any]:
        download = self.manager.add(
            url,
            filename=filename,
            headers={"Referer": str(message["referrer"])}
            if message.get("referrer")
            else None,
        )
        self.context.events.post(
            Events.BROWSER_EVENT,
            {"url": url, "source": message.get("source"), "download_id": download.id},
        )
        self._start_on_loop(download.id)
        return {
            "type": "capture_ok",
            "download_id": download.id,
            "filename": download.filename,
        }

    def _enqueue_pending_capture(
        self, message: dict[str, Any], url: str
    ) -> Any:
        with self.context.session_factory() as session:
            repo = PendingCaptureRepository(session)
            existing = [
                c
                for c in repo.pending()
                if c.url == url and (message.get("filename") or None) in (c.filename, None)
            ]
            if existing:
                return existing[0]
            return repo.add(
                url,
                filename=message.get("filename") or None,
                referrer=message.get("referrer") or None,
                source=str(message.get("source") or "browser"),
                detected_type=str(message.get("detected_type") or "file"),
            )

    def _page_scan(self, message: dict[str, Any]) -> dict[str, Any]:
        """Record downloadable files found on a page by the extension."""
        if not self.context.settings.get("browser.integration_enabled", False):
            return {"type": "page_scan_error", "message": "integration disabled"}
        page_url = str(message.get("url") or "").strip()
        files = message.get("files") or []
        if not page_url:
            return {"type": "page_scan_error", "message": "missing page url"}
        with self.context.session_factory() as session:
            repo = BrowserDetectionRepository(session)
            repo.add(page_url, count=len(files), files=files[:50])
        self.context.events.post(
            Events.BROWSER_EVENT,
            {"url": page_url, "source": "page_scan", "count": len(files)},
        )
        return {"type": "page_scan_ok", "count": len(files)}

    def _capture_result(self, message: dict[str, Any]) -> dict[str, Any]:
        """Notify the app of a user's decision made in the capture dialog."""
        return {"type": "capture_result_ok"}

    def _status(self) -> dict[str, Any]:
        active = completed = 0
        if self.manager is not None:
            snapshots = self.manager.list_snapshots(limit=2000)
            active = sum(
                1 for s in snapshots
                if s["status"] in ("connecting", "downloading", "retrying", "verifying")
            )
            completed = sum(1 for s in snapshots if s["status"] == "completed")
        return {
            "type": "status_ok",
            "app": "MagnetoClip",
            "active": active,
            "completed": completed,
            "integration_enabled": bool(
                self.context.settings.get("browser.integration_enabled", False)
            ),
            "capture_enabled": bool(
                self.context.settings.get("browser.capture_enabled", True)
            ),
        }

    def _settings(self) -> dict[str, Any]:
        return {
            "type": "settings_ok",
            "integration_enabled": bool(
                self.context.settings.get("browser.integration_enabled", False)
            ),
            "capture_enabled": bool(
                self.context.settings.get("browser.capture_enabled", True)
            ),
        }

    # ----- helpers -----

    @staticmethod
    def _validate_url(url: str) -> str | None:
        if not url:
            return "empty URL"
        try:
            parsed = httpx.URL(url)
        except Exception:  # noqa: BLE001 - any malformed URL is rejected
            return "invalid URL"
        if parsed.scheme not in ("http", "https") or not parsed.host:
            return "only http/https URLs are supported"
        return None

    def _record_event(self, message: dict[str, Any], url: str) -> None:
        try:
            with self.context.session_factory() as session:
                session.add(
                    BrowserEvent(
                        source=str(message.get("source") or "browser"),
                        url=url,
                        detected_type=str(message.get("detected_type") or "file"),
                        ts=datetime.now(UTC),
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - event logging must not break capture
            log.warning("browser_event_record_failed", error=str(exc))

    def _start_on_loop(self, download_id: int) -> None:
        if self._loop is None or self.manager is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._start_async(download_id), self._loop
            )
        except Exception:  # noqa: BLE001 - capture succeeded even if start is queued
            log.warning("browser_start_scheduled_later", download_id=download_id)

    async def _start_async(self, download_id: int) -> None:
        self.manager.start(download_id)
