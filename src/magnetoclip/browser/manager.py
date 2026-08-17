"""Browser integration manager: routes native-messaging requests to the app."""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from magnetoclip.browser.skip import skip_all_active
from magnetoclip.core.events.bus import Events
from magnetoclip.database.models import BrowserEvent
from magnetoclip.database.repositories import (
    BrowserDetectionRepository,
    BrowserRequestRepository,
    PendingCaptureRepository,
    SettingsStore,
)
from magnetoclip.media.streaming import is_streaming_url
from magnetoclip.network.content import should_reject_html_body
from magnetoclip.services.logging import get_logger

log = get_logger(__name__)

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


class BrowserManager:
    """Handles requests from the browser extension over native messaging."""

    # Native messaging messages are size-limited (Chrome: ~1MB), so large
    # in-memory media (Telegram photos, blob-backed clips) travel as base64
    # chunks. Bounded number of concurrent assemblies to cap memory use.
    _MAX_CHUNK_ASSEMBLIES = 16
    _CHUNK_EXPIRY_SECONDS = 60.0

    def __init__(self, context, manager=None) -> None:
        self.context = context
        self.manager = manager or getattr(context, "manager", None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._chunk_assemblies: dict[str, dict] = {}

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
        if message_type == "capture_chunk":
            return self._capture_chunk(message)
        if message_type == "page_scan":
            return self._page_scan(message)
        if message_type == "capture_result":
            return self._capture_result(message)
        if message_type == "blob_fetch_chunk":
            return self._blob_fetch_chunk(message)
        if message_type == "blob_fetch_result":
            return self._blob_fetch_result(message)
        return {"type": "error", "message": f"unknown message type: {message_type}"}

    # ----- handlers -----

    def _capture(self, message: dict[str, Any]) -> dict[str, Any]:
        if not self.context.settings.get("browser.integration_enabled", False):
            return {
                "type": "capture_error",
                "message": "integration disabled in MagnetoClip",
            }
        url = str(message.get("url") or "").strip()
        data_base64 = message.get("data_base64")
        if data_base64:
            # In-memory media (Telegram blob: images): no HTTP URL to validate
            # or probe — the bytes already travelled to us.
            if not url:
                return {"type": "capture_error", "message": "empty URL"}
        else:
            error = self._validate_url(url)
            if error:
                return {"type": "capture_error", "message": error}
        filename = message.get("filename") or self._filename_from_mime(
            message.get("mime_type")
        )
        self._record_event(message, url)

        if skip_all_active(self.context):
            return self._skip_capture(message, url)

        # Right-click and popup captures are explicit user actions: start the
        # download immediately, without probing or asking for confirmation.
        explicit = message.get("source") in ("context_menu", "popup")
        # Auto-detected media from social platforms always goes through the
        # confirmation dialog; it must never silently download a whole feed.
        auto_detected = message.get("source") == "page_scan"
        if not explicit and not auto_detected and not data_base64:
            probe_error = self._probe_for_capture(message, url)
            if probe_error:
                return {"type": "capture_error", "message": probe_error}
        if not explicit and (
            auto_detected or self.context.settings.get("browser.confirm_capture", True)
        ):
            capture = self._enqueue_pending_capture(message, url, data_base64)
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
        return self._start_immediately(message, url, filename, data_base64)

    def _capture_chunk(self, message: dict[str, Any]) -> dict[str, Any]:
        """Assemble a chunked in-memory capture, then run the normal capture flow.

        Large blob media (Telegram photos/videos) cannot fit in a single native
        messaging message, so the extension splits them into ``capture_chunk``
        messages sharing a ``capture_key``. Intermediate chunks answer with
        ``capture_chunk_ok``; the final chunk is joined and handed to ``_capture``,
        whose response (capture_pending/ok/error) is returned to the extension.
        """
        key = str(message.get("capture_key") or "").strip()
        if not key:
            return {"type": "capture_chunk_error", "message": "missing capture key"}
        try:
            index = int(message.get("index") or 0)
            total = int(message.get("total") or 1)
        except (TypeError, ValueError):
            return {"type": "capture_chunk_error", "message": "invalid chunk index"}
        if index < 0 or total < 1 or index >= total:
            return {"type": "capture_chunk_error", "message": "invalid chunk bounds"}
        chunk = str(message.get("chunk") or "")
        if not chunk:
            return {"type": "capture_chunk_error", "message": "empty chunk"}
        self._purge_stale_chunks()
        if len(self._chunk_assemblies) >= self._MAX_CHUNK_ASSEMBLIES:
            return {
                "type": "capture_chunk_error",
                "message": "too many concurrent captures",
            }

        state = self._chunk_assemblies.setdefault(
            key,
            {
                "chunks": {},
                "meta": self._chunk_meta(message),
                "started_at": time.monotonic(),
            },
        )
        state["chunks"][index] = chunk
        # Chunks may arrive out of order, so only finalize once every chunk is
        # in hand. Dropped chunks expire via _purge_stale_chunks.
        if len(state["chunks"]) != total:
            return {"type": "capture_chunk_ok"}

        self._chunk_assemblies.pop(key, None)
        payload = "".join(state["chunks"][i] for i in range(total))
        try:
            base64.b64decode(payload, validate=True)
        except Exception:  # noqa: BLE001 - corrupt chunks are rejected up front
            return {"type": "capture_chunk_error", "message": "corrupt capture data"}

        meta = state["meta"]
        return self._capture(
            {
                "url": meta["url"],
                "filename": meta["filename"],
                "referrer": meta["referrer"],
                "mime_type": meta["mime_type"],
                "detected_type": meta["detected_type"],
                "source": meta["source"] or "page_scan",
                "data_base64": payload,
            }
        )

    def _purge_stale_chunks(self) -> None:
        """Drop chunk assemblies that never completed (dropped chunks)."""
        now = time.monotonic()
        for key in list(self._chunk_assemblies):
            if now - self._chunk_assemblies[key]["started_at"] > self._CHUNK_EXPIRY_SECONDS:
                if key.startswith("blob-fetch:"):
                    self._mark_blob_fetch_error(
                        key.removeprefix("blob-fetch:"), "timed out while fetching"
                    )
                del self._chunk_assemblies[key]

    @staticmethod
    def _chunk_meta(message: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": message.get("url"),
            "filename": message.get("filename"),
            "referrer": message.get("referrer"),
            "mime_type": message.get("mime_type"),
            "detected_type": message.get("detected_type"),
            "source": message.get("source") or "page_scan",
        }

    # ----- app->extension requests (blob: URL fetches) -----

    def next_outbound_message(self) -> dict[str, Any] | None:
        """Return the next queued app request to push to the extension.

        Called by the native-messaging host's writer thread; ``None`` means
        nothing to send. Only ``blob:`` fetch requests are supported.
        """
        if not self.context.settings.get("browser.integration_enabled", False):
            return None
        try:
            with self.context.session_factory() as session:
                request = BrowserRequestRepository(session).next_queued()
        except Exception:  # noqa: BLE001 - a broken request row must not kill the host
            log.warning("browser_request_poll_failed", exc_info=True)
            return None
        if request is None:
            return None
        payload = request.payload_json or {}
        if request.type != "fetch_blob" or not payload.get("url"):
            self._mark_blob_fetch_error(request.id, "unsupported request type")
            return None
        return {
            "type": "fetch_blob",
            "request_id": request.id,
            "url": str(payload["url"]),
        }

    def _blob_fetch_chunk(self, message: dict[str, Any]) -> dict[str, Any]:
        """Reassemble blob bytes streamed back by the extension for a fetch."""
        request_id = str(message.get("request_id") or "").strip()
        if not request_id or not request_id.isdigit():
            return {"type": "blob_fetch_error", "message": "missing request id"}
        index = message.get("index")
        total = message.get("total")
        chunk = message.get("chunk")
        try:
            index = int(index or 0)
            total = int(total or 1)
        except (TypeError, ValueError):
            return {"type": "blob_fetch_error", "message": "invalid chunk index"}
        if index < 0 or total < 1 or index >= total:
            return {"type": "blob_fetch_error", "message": "invalid chunk bounds"}
        if not chunk:
            return {"type": "blob_fetch_error", "message": "empty chunk"}
        self._purge_stale_chunks()
        if len(self._chunk_assemblies) >= self._MAX_CHUNK_ASSEMBLIES:
            return {"type": "blob_fetch_error", "message": "too many concurrent fetches"}

        key = f"blob-fetch:{request_id}"
        state = self._chunk_assemblies.setdefault(
            key, {"chunks": {}, "meta": {}, "started_at": time.monotonic()}
        )
        state["chunks"][index] = str(chunk)
        if len(state["chunks"]) != total:
            return {"type": "blob_fetch_chunk_ok"}
        self._chunk_assemblies.pop(key, None)
        payload = "".join(state["chunks"][i] for i in range(total))
        try:
            base64.b64decode(payload, validate=True)
        except Exception:  # noqa: BLE001 - corrupt chunks are rejected up front
            self._mark_blob_fetch_error(request_id, "corrupt blob data")
            return {"type": "blob_fetch_error", "message": "corrupt blob data"}
        try:
            with self.context.session_factory() as session:
                repo = BrowserRequestRepository(session)
                if not repo.resolve_data(
                    int(request_id),
                    data_base64=payload,
                    meta={
                        "filename": str(message.get("filename") or ""),
                        "mime_type": str(message.get("mime_type") or ""),
                    },
                ):
                    return {"type": "blob_fetch_error", "message": "unknown request"}
        except Exception:  # noqa: BLE001 - DB errors surface to the caller
            log.warning("blob_fetch_store_failed", request_id=request_id, exc_info=True)
            return {"type": "blob_fetch_error", "message": "could not store blob data"}
        self.context.events.post(
            Events.BROWSER_EVENT,
            {"url": message.get("url") or "", "source": "blob_fetch", "request_id": int(request_id)},
        )
        return {"type": "blob_fetch_chunk_ok"}

    def _blob_fetch_result(self, message: dict[str, Any]) -> dict[str, Any]:
        """Record a fetch failure reported by the extension."""
        request_id = str(message.get("request_id") or "").strip()
        error = str(message.get("error") or "could not fetch the blob from the browser")
        if request_id and request_id.isdigit():
            self._mark_blob_fetch_error(int(request_id), error)
        return {"type": "blob_fetch_result_ok"}

    def _mark_blob_fetch_error(self, request_id: int | str, message: str) -> None:
        try:
            with self.context.session_factory() as session:
                BrowserRequestRepository(session).mark_error(int(request_id), message)
        except Exception:  # noqa: BLE001 - failing to record an error is not fatal
            log.warning("blob_fetch_error_record_failed", request_id=request_id, exc_info=True)

    def _start_immediately(
        self,
        message: dict[str, Any],
        url: str,
        filename: str | None,
        data_base64: str | None = None,
    ) -> dict[str, Any]:
        download = self.manager.add(
            url,
            filename=filename,
            data=self._decode_data(data_base64),
            headers={"Referer": str(message["referrer"])}
            if message.get("referrer")
            else None,
            cookies=self._parse_cookies(message.get("cookies")),
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
        self, message: dict[str, Any], url: str, data_base64: str | None = None
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
                filename=message.get("filename")
                or self._filename_from_mime(message.get("mime_type"))
                or None,
                referrer=message.get("referrer") or None,
                source=str(message.get("source") or "browser"),
                detected_type=str(message.get("detected_type") or "file"),
                cookies=self._parse_cookies(message.get("cookies")),
                data_base64=data_base64,
            )

    def _skip_capture(self, message: dict[str, Any], url: str) -> dict[str, Any]:
        """Record a capture that the user asked to skip without being asked again."""
        with self.context.session_factory() as session:
            repo = PendingCaptureRepository(session)
            capture = repo.add(
                url,
                filename=message.get("filename") or None,
                referrer=message.get("referrer") or None,
                source=str(message.get("source") or "browser"),
                detected_type=str(message.get("detected_type") or "file"),
                cookies=self._parse_cookies(message.get("cookies")),
                status="rejected",
            )
        self.context.events.post(
            Events.BROWSER_EVENT,
            {
                "url": url,
                "source": message.get("source"),
                "pending_capture_id": capture.id,
                "rejected": True,
            },
        )
        return {"type": "capture_skipped", "id": capture.id, "url": url}

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
        self._refresh_settings()
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
            "default_downloader": bool(
                self.context.settings.get("browser.default_downloader", False)
            ),
        }

    def _settings(self) -> dict[str, Any]:
        self._refresh_settings()
        return {
            "type": "settings_ok",
            "integration_enabled": bool(
                self.context.settings.get("browser.integration_enabled", False)
            ),
            "capture_enabled": bool(
                self.context.settings.get("browser.capture_enabled", True)
            ),
            "default_downloader": bool(
                self.context.settings.get("browser.default_downloader", False)
            ),
        }

    # ----- helpers -----

    def _refresh_settings(self) -> None:
        """Reload settings persisted by the app.

        The native-messaging host builds its settings snapshot once at startup;
        the app keeps the single source of truth in the ``settings`` table, so
        every settings/status request re-merges the stored values before
        answering the extension. Failures leave the in-memory copy unchanged.
        """
        try:
            stored = SettingsStore(self.context.session_factory).load_all()
        except Exception:  # noqa: BLE001 - stale settings must not break requests
            log.warning("browser_settings_refresh_failed", exc_info=True)
            return
        self.context.settings.merge(stored)

    @staticmethod
    def _parse_cookies(value) -> dict[str, str] | None:
        """Accept a cookie header string or dict and return ``{name: value}``."""
        if not value:
            return None
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items() if v is not None}
        if isinstance(value, str):
            from magnetoclip.network.cookies.jar import parse_cookie_header

            return parse_cookie_header(value)
        return None

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

    _MIME_EXTENSIONS = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
        "image/svg+xml": "svg",
        "image/tiff": "tiff",
        "image/avif": "avif",
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/ogg": "ogv",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
    }

    @classmethod
    def _filename_from_mime(cls, mime_type: str | None) -> str | None:
        """Build a sensible filename from a media MIME type."""
        if not mime_type:
            return None
        ext = cls._MIME_EXTENSIONS.get(str(mime_type).split(";")[0].strip().lower())
        if ext:
            return f"captured-media.{ext}"
        return None

    @staticmethod
    def _decode_data(data_base64: str | None) -> bytes | None:
        """Decode inline capture data; return None when absent or corrupt."""
        if not data_base64:
            return None
        try:
            return base64.b64decode(data_base64, validate=True)
        except Exception:  # noqa: BLE001 - corrupt data becomes a normal download
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

    def _probe_for_capture(
        self, message: dict[str, Any], url: str
    ) -> str | None:
        """Validate a captured URL; return an error message or None.

        Runs the probe on the app loop so the native host thread can block for
        the result. Skipped when called from the loop's own thread (e.g. from
        in-process tests) to avoid deadlocking; the engine re-validates at
        download time either way.
        """
        if self._loop is None:
            return None
        try:
            if asyncio.get_running_loop() is self._loop:
                return None
        except RuntimeError:
            pass
        headers = {}
        if message.get("referrer"):
            headers["Referer"] = str(message["referrer"])
        future = asyncio.run_coroutine_threadsafe(
            self._probe_url(
                url,
                headers=headers,
                cookies=self._parse_cookies(message.get("cookies")),
            ),
            self._loop,
        )
        try:
            result = future.result(timeout=20.0)
        except TimeoutError:
            return "timed out while checking the URL"
        except Exception as exc:  # noqa: BLE001 - probe plumbing must not crash
            return f"could not check the URL: {exc}"
        if isinstance(result, Exception):
            return self._probe_error_message(result)
        # Streaming pages (YouTube, Facebook posts, ...) legitimately answer an
        # HTML shell to a bare GET; rejecting them would break right-click and
        # link captures that yt-dlp resolves into real media later.
        if not is_streaming_url(url) and should_reject_html_body(
            result.content_type, message.get("filename") or url
        ):
            return (
                "server returned an HTML page instead of the requested file "
                "(the link may be broken, removed, or behind a login)"
            )
        return None

    @staticmethod
    def _probe_error_message(exc: Exception) -> str:
        """Turn a probe failure into a user-facing message."""
        status: int | None = None
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            status = exc.response.status_code
        if status is None:
            match = _HTTP_STATUS_RE.search(str(exc))
            if match:
                status = int(match.group(1))
        if status is not None:
            return (
                "server responded with HTTP "
                f"{status} (the link may be broken or require a login)"
            )
        return f"could not check the URL: {exc}"

    async def _probe_url(
        self,
        url: str,
        *,
        headers: dict[str, str],
        cookies: dict[str, str] | None,
    ) -> Any:
        """Fetch minimal metadata for a capture URL on the app loop."""
        from magnetoclip.engine.downloader.engine import analyze
        from magnetoclip.network.http.client import ClientConfig, build_client

        client = build_client(ClientConfig(cookies=cookies or {}))
        try:
            return await analyze(
                client, url, headers=headers or None, timeout=10.0
            )
        except Exception as exc:  # noqa: BLE001 - probe failures become error text
            return exc
        finally:
            await client.aclose()

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
