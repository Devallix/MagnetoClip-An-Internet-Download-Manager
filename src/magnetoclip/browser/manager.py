"""Browser integration manager: routes native-messaging requests to the app."""

from __future__ import annotations

import asyncio
import base64
import json
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


def _is_youtube_video_url(url: str) -> bool:
    """True when *url* is a YouTube video (or a video inside a playlist).

    Used to route captures through the quality-dialog path — the native
    messaging host cannot show Qt dialogs, and silently downloading at the
    global "best" default silently skips resolution selection.
    """
    from magnetoclip.media.youtube.detector import YouTubeURLDetector
    from magnetoclip.media.youtube.types import YouTubeURLType

    try:
        return YouTubeURLDetector.classify(url) in (
            YouTubeURLType.video,
            YouTubeURLType.video_with_playlist,
        )
    except Exception:  # noqa: BLE001 - a bad URL simply is not a YouTube video
        return False


class BrowserManager:
    """Handles requests from the browser extension over native messaging."""

    # Native messaging messages are size-limited (Chrome: ~1MB), so large
    # in-memory media (Telegram photos, blob-backed clips) travel as base64
    # chunks. Bounded number of concurrent assemblies to cap memory use.
    _MAX_CHUNK_ASSEMBLIES = 16
    _CHUNK_EXPIRY_SECONDS = 60.0
    # All chunked media transfers are capped at ~480 MiB of base64 (~360 MiB
    # of raw media). This applies to known-total blob captures as well as live
    # Telegram stream captures, preventing a sender from bypassing the memory
    # bound merely by declaring the chunk count in advance.
    _CHUNK_CAPTURE_MAX_BASE64 = 480 * 1024 * 1024
    _STREAM_CAPTURE_MAX_BASE64 = _CHUNK_CAPTURE_MAX_BASE64

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
        if message_type == "archive":
            return self._archive(message)
        if message_type == "page_scan":
            return self._page_scan(message)
        if message_type == "detection_sync":
            return self._detection_sync(message)
        if message_type == "open_detected":
            return self._open_detected(message)
        if message_type == "download_detection":
            return self._download_detection(message)
        if message_type == "capture_result":
            return self._capture_result(message)
        if message_type == "blob_fetch_chunk":
            return self._blob_fetch_chunk(message)
        if message_type == "blob_fetch_result":
            return self._blob_fetch_result(message)
        if message_type == "diag":
            return self._diag(message)
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
        log.info(
            "capture_received",
            url=url,
            source=message.get("source") or "",
            detected_type=message.get("detected_type") or "",
            has_data=bool(data_base64),
        )
        # Scope guard for downloads the extension auto-intercept may offer
        # before its own settings snapshot has re-synced (service-worker
        # restarts reset its in-memory flags to the defaults, so it can claim
        # a non-media file even when MagnetoClip is not the default
        # downloader). Honor the real settings here: refuse the capture so the
        # browser keeps the file.
        from_extension = message.get("source") == "extension"
        if from_extension and not self.context.settings.get(
            "browser.default_downloader", False
        ):
            capture_enabled = self.context.settings.get("browser.capture_enabled", True)
            if not capture_enabled or not self._is_media_capture(message):
                return {"type": "capture_skipped", "url": url}
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

        # Right-click and popup captures are explicit user actions. They
        # ALWAYS go through the confirmation dialog (pre-filled with the
        # captured URL) so the user can review and adjust the download before
        # it starts — silently auto-starting can produce confusing failures
        # (e.g. 403) on hotlinked media such as images from Wikimedia.
        explicit = message.get("source") in ("context_menu", "popup")
        if explicit:
            return self._queue_pending(message, url, data_base64)
        # Auto-detected media from social platforms always goes through the
        # confirmation dialog; it must never silently download a whole feed.
        auto_detected = message.get("source") == "page_scan"
        # When MagnetoClip is set as the default downloader, extension-
        # intercepted downloads bypass the probe.  The browser already
        # validated the URL by starting the download; probing again can
        # reject legitimate files (e.g. HTML error pages from CDNs that
        # actually serve the file, or URLs behind redirects).
        default_downloader = self.context.settings.get(
            "browser.default_downloader", False
        )
        skip_probe = default_downloader and from_extension
        if not auto_detected and not data_base64 and not skip_probe:
            probe_error = self._probe_for_capture(message, url)
            if probe_error:
                return {"type": "capture_error", "message": probe_error}
        if auto_detected:
            return self._queue_pending(message, url, data_base64)
        # When MagnetoClip is the default downloader, extension-intercepted
        # downloads start immediately — the user already committed to the
        # download by clicking the link/button in the browser.
        if self.context.settings.get("browser.confirm_capture", True) and not (
            default_downloader and from_extension
        ):
            return self._queue_pending(message, url, data_base64)

        # YouTube video URLs always go through the confirmation + resolution
        # dialog path: the native-messaging host cannot show Qt dialogs, and
        # silently downloading at the global "best" default is why
        # resolution-selection was being skipped. The UI offers the quality
        # picker once the capture reaches the queue.
        if self.manager is not None and _is_youtube_video_url(url):
            return self._queue_pending(message, url, data_base64)

        if self.manager is None:
            return self._queue_pending(message, url, data_base64)
        return self._start_immediately(message, url, filename, data_base64)

    def _queue_pending(
        self, message: dict[str, Any], url: str, data_base64: str | None
    ) -> dict[str, Any]:
        capture = self._enqueue_pending_capture(message, url, data_base64)
        log.info(
            "capture_queued_pending",
            capture_id=capture.id,
            url=url,
            source=message.get("source"),
            filename=capture.filename,
        )
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

    def _capture_chunk(self, message: dict[str, Any]) -> dict[str, Any]:
        """Assemble a chunked in-memory capture, then run the normal capture flow.

        Large blob media (Telegram photos/videos) cannot fit in a single native
        messaging message, so the extension splits them into ``capture_chunk``
        messages sharing a ``capture_key``. Intermediate chunks answer with
        ``capture_chunk_ok``; the final chunk is joined and handed to ``_capture``,
        whose response (capture_pending/ok/error) is returned to the extension.

        ``total`` may also be ``0`` for *streaming* captures (a live Telegram
        Web proxy link streamed to the app at capture time): ``total`` is
        unknown up front, so chunks are appended in order and the assembly is
        finalized by the ``last`` flag instead of a chunk count.
        """
        key = str(message.get("capture_key") or "").strip()
        if not key:
            return {"type": "capture_chunk_error", "message": "missing capture key"}
        try:
            index = int(message.get("index") or 0)
            total = int(message.get("total") or 0)
        except (TypeError, ValueError):
            return {"type": "capture_chunk_error", "message": "invalid chunk index"}
        streaming = total == 0
        if not streaming and (index < 0 or total < 1 or index >= total):
            return {"type": "capture_chunk_error", "message": "invalid chunk bounds"}
        chunk = str(message.get("chunk") or "")
        if not streaming and not chunk:
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
                "sequence": [],
                "sequence_len": 0,
                "total_chars": 0,
                "meta": self._chunk_meta(message),
                "started_at": time.monotonic(),
            },
        )
        if streaming:
            # Streaming chunks must arrive in strict order so the served bytes
            # are contiguous; out-of-order delivery aborts the stream.
            if index != state["sequence_len"]:
                return {
                    "type": "capture_chunk_error",
                    "message": "stream chunk out of order",
                }
            if chunk:
                state["sequence"].append(chunk)
                state["sequence_len"] += 1
                state["total_chars"] += len(chunk)
        else:
            # Account for duplicate retries safely, then enforce the same
            # aggregate limit used by unknown-length live stream captures.
            previous = state["chunks"].get(index)
            if previous is not None:
                state["total_chars"] -= len(previous)
            state["chunks"][index] = chunk
            state["total_chars"] += len(chunk)
        if state["total_chars"] > self._CHUNK_CAPTURE_MAX_BASE64:
            self._chunk_assemblies.pop(key, None)
            return {
                "type": "capture_chunk_error",
                "message": "capture is too large",
            }
        # Chunks arrive over a span (large Telegram media streams for seconds),
        # so the assembly expiry is an inactivity timeout, not a wall-clock cap:
        # refresh it on every chunk so a slow but alive transfer survives.
        state["started_at"] = time.monotonic()
        if not streaming and len(state["chunks"]) != total:
            return {"type": "capture_chunk_ok"}
        if streaming and not message.get("last"):
            if state["total_chars"] >= self._STREAM_CAPTURE_MAX_BASE64:
                return {
                    "type": "capture_chunk_error",
                    "message": "capture is too large",
                }
            return {"type": "capture_chunk_ok"}

        if streaming and not state["sequence"]:
            self._chunk_assemblies.pop(key, None)
            return {"type": "capture_chunk_error", "message": "empty capture"}
        if streaming and state["total_chars"] >= self._STREAM_CAPTURE_MAX_BASE64:
            self._chunk_assemblies.pop(key, None)
            return {"type": "capture_chunk_error", "message": "capture is too large"}
        self._chunk_assemblies.pop(key, None)
        if streaming:
            payload = "".join(state["sequence"])
        else:
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

    def _archive(self, message: dict[str, Any]) -> dict[str, Any]:
        """Fetch and inline a webpage into a self-contained .html file.

        Triggered by the extension's "Archive this page" action. Archiving runs
        entirely in the app (the archiver fetches the page server-side), so no
        bytes travel over native messaging — only the URL (+ cookies/referrer).
        """
        if not self.context.settings.get("browser.integration_enabled", False):
            return {
                "type": "archive_error",
                "message": "integration disabled in MagnetoClip",
            }
        if not self.context.settings.get("archiver.enabled", True):
            return {
                "type": "archive_error",
                "message": "webpage archiving is disabled in MagnetoClip",
            }
        url = str(message.get("url") or "").strip()
        error = self._validate_url(url)
        if error:
            return {"type": "archive_error", "message": error}
        self._record_event(message, url)

        cookies = self._parse_cookies(message.get("cookies"))
        referrer = str(message.get("referrer") or "")
        try:
            download = self.manager.add(
                url,
                cookies=cookies,
                headers={"Referer": referrer} if referrer else None,
                archive=True,
            )
        except Exception as exc:  # noqa: BLE001 - surface archive failures to the extension
            log.warning("browser_archive_failed url=%s error=%s", url, str(exc))
            return {
                "type": "archive_error",
                "message": f"could not start archiving: {exc}",
            }
        self.context.events.post(
            Events.BROWSER_EVENT,
            {
                "url": url,
                "source": message.get("source") or "context_menu",
                "download_id": download.id,
                "archive": True,
            },
        )
        return {
            "type": "archive_ok",
            "download_id": download.id,
            "filename": download.filename,
        }

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
            "headers": message.get("headers") or {},
            "page_url": message.get("page_url"),
            "frame_url": message.get("frame_url"),
            "mime_type": message.get("mime_type"),
            "detected_type": message.get("detected_type"),
            "source": message.get("source") or "page_scan",
        }

    # ----- app->extension requests (blob: URL fetches) -----

    # Request types the host writer may claim. Anything else in the table is
    # consumed by the app process (e.g. "navigate" is polled by the UI).
    OUTBOUND_REQUEST_TYPES = ("fetch_blob", "detections_handled", "fetch_detections")

    def next_outbound_message(self) -> dict[str, Any] | None:
        """Return the next queued app request to push to the extension.

        Called by the native-messaging host's writer thread; ``None`` means
        nothing to send. ``blob:`` fetches, detection-queue acknowledgements and
        detection-queue pulls are supported.
        """
        if not self.context.settings.get("browser.integration_enabled", False):
            return None
        try:
            with self.context.session_factory() as session:
                request = BrowserRequestRepository(session).next_queued(
                    list(self.OUTBOUND_REQUEST_TYPES)
                )
        except Exception:  # noqa: BLE001 - a broken request row must not kill the host
            log.warning("browser_request_poll_failed", exc_info=True)
            return None
        if request is None:
            return None
        payload = request.payload_json or {}
        if request.type == "detections_handled":
            urls = [str(u) for u in payload.get("urls") or [] if str(u or "").strip()]
            if not urls:
                self._mark_blob_fetch_error(request.id, "no urls to acknowledge")
                return None
            message: dict[str, Any] = {"type": "detections_handled", "urls": urls}
            ids = [str(i) for i in payload.get("ids") or [] if str(i or "").strip()]
            if ids:
                message["ids"] = ids
            return message
        if request.type == "fetch_detections":
            return {"type": "fetch_detections", "request_id": request.id}
        if request.type != "fetch_blob" or not payload.get("url"):
            self._mark_blob_fetch_error(request.id, "unsupported request type")
            return None
        message = {
            "type": "fetch_blob",
            "request_id": request.id,
            "url": str(payload["url"]),
        }
        if payload.get("background"):
            # Service-worker fetch (HLS behind Cloudflare): no tab exists for
            # the CDN origin, so the extension fetches from its own context.
            message["background"] = True
        if payload.get("referrer"):
            message["referrer"] = str(payload["referrer"])
        if payload.get("frame_url"):
            message["frame_url"] = str(payload["frame_url"])
        if isinstance(payload.get("headers"), dict):
            message["headers"] = {str(k): str(v) for k, v in payload["headers"].items()}
        return message

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
        # Same inactivity-based expiry as capture chunks: a large file takes
        # seconds to stream, and purging mid-transfer would lose the download.
        state["started_at"] = time.monotonic()
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
        """Record a fetch result reported by the extension.

        Three flavours: an error (``error``), a successful resolution to a
        direct URL (``resolved_url``) with the session cookies needed to fetch
        it — used for Telegram Web proxy links that redirect to a CDN file the
        page can legally fetch but the app cannot reach directly — or a finished
        file the browser downloaded itself (``external_path``), used when a
        Telegram stream link is one-shot and must be fetched by the browser
        while on screen.
        """
        request_id = str(message.get("request_id") or "").strip()
        if not request_id or not request_id.isdigit():
            return {"type": "blob_fetch_result_ok"}
        request_id_int = int(request_id)
        error = message.get("error")
        if error:
            self._mark_blob_fetch_error(request_id_int, str(error))
            return {"type": "blob_fetch_result_ok"}
        external_path = str(message.get("external_path") or "").strip()
        if external_path:
            self._store_blob_result(
                request_id_int,
                {
                    "external_path": external_path,
                    "filename": str(message.get("filename") or ""),
                    "mime_type": str(message.get("mime_type") or ""),
                    "source_url": str(message.get("source_url") or ""),
                },
            )
            return {"type": "blob_fetch_result_ok"}
        resolved_url = str(message.get("resolved_url") or "").strip()
        if resolved_url:
            self._store_blob_result(
                request_id_int,
                {
                    "resolved_url": resolved_url,
                    "cookies": message.get("cookies") or "",
                    "referrer": str(message.get("referrer") or ""),
                    "mime_type": str(message.get("mime_type") or ""),
                },
            )
            return {"type": "blob_fetch_result_ok"}
        return {"type": "blob_fetch_result_ok"}

    def _store_blob_result(self, request_id: int, meta: dict[str, Any]) -> None:
        try:
            with self.context.session_factory() as session:
                BrowserRequestRepository(session).resolve_data(request_id, meta=meta)
        except Exception:  # noqa: BLE001 - DB errors surface to the caller
            log.warning("blob_fetch_store_failed", request_id=request_id, exc_info=True)

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
        from ..media.models import RequestContext

        cookies = self._parse_cookies(message.get("cookies"))
        captured_headers = {
            str(k): str(v)
            for k, v in (message.get("headers") or {}).items()
            if v is not None
        }
        if message.get("referrer"):
            captured_headers.setdefault("Referer", str(message["referrer"]))
        if message.get("user_agent"):
            captured_headers.setdefault("User-Agent", str(message["user_agent"]))
        download = self.manager.add(
            url,
            filename=filename,
            data=self._decode_data(data_base64),
            headers=captured_headers or None,
            cookies=cookies,
            request_context=RequestContext.from_headers(
                captured_headers,
                cookies=cookies,
                user_agent=message.get("user_agent") or None,
                page_url=message.get("page_url") or None,
                frame_url=message.get("frame_url") or message.get("referrer") or None,
                source=str(message.get("source") or "browser"),
            ),
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
                user_agent=message.get("user_agent") or None,
                source=str(message.get("source") or "browser"),
                detected_type=str(message.get("detected_type") or "file"),
                headers={str(k): str(v) for k, v in (message.get("headers") or {}).items() if v is not None},
                cookies=self._parse_cookies(message.get("cookies")),
                data_base64=data_base64,
                page_url=message.get("page_url") or None,
                frame_url=message.get("frame_url") or None,
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
                headers={str(k): str(v) for k, v in (message.get("headers") or {}).items() if v is not None},
                cookies=self._parse_cookies(message.get("cookies")),
                status="rejected",
                page_url=message.get("page_url") or None,
                frame_url=message.get("frame_url") or None,
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
        return {
            "type": "capture_skipped",
            "id": capture.id,
            "url": url,
            "message": "'Skip all' is active in MagnetoClip — re-enable captures from its Browser settings.",
        }

    def _page_scan(self, message: dict[str, Any]) -> dict[str, Any]:
        """Record downloadable files found on a page by the extension."""
        if not self.context.settings.get("browser.integration_enabled", False):
            return {"type": "page_scan_error", "message": "integration disabled"}
        page_url = str(message.get("url") or "").strip()
        files = message.get("files") or []
        if not page_url:
            return {"type": "page_scan_error", "message": "missing page url"}
        log.info(
            "page_scan_received",
            url=page_url,
            count=len(files),
            source="extension",
        )
        # Blob/data URIs are page-bound (only the creating tab can resolve
        # them); their bytes already travelled through the capture path, and
        # keeping them here would re-notify on every rescan because Telegram
        # mints a fresh UUID per render.
        files = [
            f
            for f in files
            if not str(f.get("url") or "").startswith(("blob:", "data:"))
        ]
        with self.context.session_factory() as session:
            repo = BrowserDetectionRepository(session)
            known = repo.known_file_urls()
            normalized = [
                self._detection_file(
                    dict(f, page_url=f.get("page_url") or page_url),
                    str(f.get("url") or "").strip(),
                    str(f.get("category") or "").strip().lower(),
                    page_url,
                )
                for f in files
            ]
            normalized = [
                f
                for f in normalized
                if f["category"] in self._DETECTION_CATEGORIES or f["detected_type"] != "other"
            ]
            has_new = any(f["url"] not in known for f in normalized)
            repo.add(page_url, count=len(normalized), files=normalized[:50], notified=not has_new)
        self.context.events.post(
            Events.BROWSER_EVENT,
            {"url": page_url, "source": "page_scan", "count": len(files)},
        )
        return {"type": "page_scan_ok", "count": len(files)}

    # Categories the extension can report. Kept in sync with
    # magnetoclip.ui.categories.DETECTION_CATEGORIES and categories.js.
    _DETECTION_CATEGORIES = (
        "images",
        "videos",
        "audio",
        "compressed",
        "documents",
        "other",
    )
    _DETECTION_MAX_FILES_PER_SYNC = 500

    def _detection_sync(self, message: dict[str, Any]) -> dict[str, Any]:
        """Store a batch of queued detections from the extension.

        Detection is now a queue: the extension classifies each resource into one
        of the six categories, keeps it in ``storage.local`` and syncs it here in
        bounded batches. Nothing in this path starts a download or opens a
        capture dialog — the entries only become Detected-page rows that the
        user acts on.
        """
        if not self.context.settings.get("browser.integration_enabled", False):
            return {"type": "detection_sync_error", "message": "integration disabled"}
        raw = message.get("detections")
        if not isinstance(raw, list):
            return {"type": "detection_sync_error", "message": "missing detections"}
        entries = [item for item in raw if isinstance(item, dict)]
        if not entries:
            return {"type": "detection_sync_ok", "stored": 0, "added": 0, "urls": []}

        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            # blob:/data: URIs are page-bound and only resolvable while the tab
            # that made them lives; they are only useful inline (data_base64).
            if url.startswith(("blob:", "data:")) and not entry.get("data_base64"):
                continue
            category = str(entry.get("category") or "").strip().lower()
            if category not in self._DETECTION_CATEGORIES:
                category = ""
            page_url = str(entry.get("page_url") or "").strip() or url
            files = grouped.setdefault(page_url, [])
            files.append(self._detection_file(entry, url, category, page_url))

        stored_urls: list[str] = []
        added = 0
        with self.context.session_factory() as session:
            repo = BrowserDetectionRepository(session)
            for page_url, files in grouped.items():
                _detection, page_added, urls = repo.merge_files(
                    page_url,
                    files,
                    max_files=self._DETECTION_MAX_FILES_PER_SYNC,
                )
                added += page_added
                stored_urls.extend(urls)
        log.info(
            "detection_sync_received",
            received=len(entries),
            pages=len(grouped),
            added=added,
        )
        self.context.events.post(
            Events.BROWSER_EVENT,
            {"url": "", "source": "detection_sync", "count": len(entries)},
        )
        return {
            "type": "detection_sync_ok",
            "received": len(entries),
            "stored": len(stored_urls),
            "added": added,
            "pages": len(grouped),
            "urls": stored_urls,
        }

    def _download_detection(self, message: dict[str, Any]) -> dict[str, Any]:
        """Start an explicit download from the player-overlay button.

        The extension resolves the queued detection to a canonical id and sends
        the full entry here. The id is the trust anchor: the app only accepts a
        download that the extension could actually associate with a queue entry,
        never a bare URL on its own.
        """
        if not self.context.settings.get("browser.integration_enabled", False):
            return {
                "type": "download_detection_error",
                "message": "integration disabled in MagnetoClip",
            }
        detection_id = str(message.get("detection_id") or "").strip()
        detection = message.get("detection")
        if not detection_id:
            return {"type": "download_detection_error", "message": "missing detection id"}
        if not isinstance(detection, dict):
            return {"type": "download_detection_error", "message": "missing detection"}
        url = str(detection.get("url") or "").strip()
        data_base64 = detection.get("data_base64") or None
        referrer = str(detection.get("referrer") or "").strip()
        frame_url = str(detection.get("frame_url") or "").strip() or referrer
        if not url and not data_base64:
            return {"type": "download_detection_error", "message": "empty URL"}
        if url and not url.startswith("data:") and not url.startswith("blob:"):
            error = self._validate_url(url)
            if error:
                return {"type": "download_detection_error", "message": error}
        if not url:
            # In-memory media (blob-backed) has nothing to download from.
            return {
                "type": "download_detection_error",
                "message": "media has no URL to download",
            }
        page_url = str(detection.get("page_url") or "").strip() or url
        category = str(detection.get("category") or "").strip().lower()
        if category not in self._DETECTION_CATEGORIES:
            from magnetoclip.ui.categories import normalize_detection_category

            category = normalize_detection_category(
                str(detection.get("detected_type") or "video")
            )
            if category not in self._DETECTION_CATEGORIES:
                category = "other"
        file_entry = self._detection_file(detection, url, category, page_url)
        try:
            with self.context.session_factory() as session:
                BrowserDetectionRepository(session).merge_files(
                    page_url,
                    [file_entry],
                    max_files=self._DETECTION_MAX_FILES_PER_SYNC,
                )
        except Exception:  # noqa: BLE001 - a DB failure must not block the download
            log.warning("download_detection_store_failed", exc_info=True)
        log.info(
            "download_detection_received",
            detection_id=detection_id,
            url=url,
            category=category,
        )
        self._record_event(detection, url)
        if skip_all_active(self.context):
            return {
                "type": "download_detection_skipped",
                "message": "downloads are paused in MagnetoClip (Skip all)",
            }

        # A player click is an explicit download request: start it immediately,
        # mirroring the Detected page's Download action. YouTube goes through
        # the quality dialog because the native host cannot offer one.
        flat = {
            "url": url,
            "filename": detection.get("filename"),
            "referrer": referrer,
            "frame_url": frame_url,
            "page_url": page_url,
            "mime_type": detection.get("mime_type"),
            "detected_type": detection.get("detected_type") or "video",
            "source": "player_button",
            "headers": detection.get("headers"),
            "cookies": detection.get("cookies"),
            "user_agent": detection.get("user_agent"),
            "data_base64": data_base64,
        }
        if _is_youtube_video_url(url):
            pending = self._queue_pending(flat, url, data_base64)
            return {
                "type": "download_detection_pending",
                "download_id": pending.get("id"),
                "message": "",
            }

        # Stream manifests must be started by the GUI-side DownloadManager,
        # not by the native-messaging host's private DownloadManager. The
        # extension popup's Open flow already hands detections to the main app
        # process; player-button streams now use the same cross-process route
        # and let the normal Detected-page download code add/start the file.
        if is_streaming_url(url):
            return self._queue_player_download(file_entry, url, page_url)

        return self._start_download_detection(flat, url, data_base64)

    def _queue_player_download(
        self, entry: dict[str, Any], url: str, page_url: str
    ) -> dict[str, Any]:
        """Queue a player stream for the GUI process to start normally.

        The native-messaging host is a separate process from the visible
        MagnetoClip window. Persist both the resolved detection and a start
        request so the UI-side CaptureWatcher can call the same download code
        used by the Detected page.
        """
        try:
            with self.context.session_factory() as session:
                BrowserRequestRepository(session).add(
                    "player_download",
                    payload={
                        "url": url,
                        "page_url": page_url,
                        "entry": entry,
                    },
                )
                # Keep the existing taskbar/navigation behavior: the UI-side
                # watcher will process the start request before this navigation
                # request, so the Downloads page opens after the row exists.
                BrowserRequestRepository(session).add(
                    "navigate",
                    payload={"page": "downloads"},
                )
        except Exception as exc:  # noqa: BLE001 - queue failure is user-visible
            log.warning(
                "player_download_queue_failed",
                url=url,
                error=str(exc),
                exc_info=True,
            )
            return {
                "type": "download_detection_error",
                "message": "could not queue the stream for MagnetoClip",
            }

        log.info(
            "player_download_queued_for_gui",
            url=url,
            page_url=page_url,
        )
        return {
            "type": "download_detection_pending",
            "download_id": 0,
            "message": "queued",
        }

    def _start_download_detection(
        self,
        flat: dict[str, Any],
        url: str,
        data_base64: str | None,
    ) -> dict[str, Any]:
        from ..media.models import RequestContext

        cookies = self._parse_cookies(flat.get("cookies"))
        headers = {
            str(k): str(v)
            for k, v in (flat.get("headers") or {}).items()
            if v is not None
        }
        if flat.get("referrer"):
            headers.setdefault("Referer", str(flat["referrer"]))
        if flat.get("user_agent"):
            headers.setdefault("User-Agent", str(flat["user_agent"]))
        download = self.manager.add(
            url,
            filename=flat.get("filename") or None,
            data=self._decode_data(data_base64),
            headers=headers or None,
            cookies=cookies,
            request_context=RequestContext.from_headers(
                headers,
                cookies=cookies,
                user_agent=flat.get("user_agent") or None,
                page_url=flat.get("page_url") or None,
                frame_url=flat.get("frame_url") or None,
                source="player_button",
            ),
        )
        log.info(
            "download_detection_started",
            download_id=download.id,
            url=url,
            source="player_button",
        )
        # The player button is clicked in the browser while MagnetoClip may be
        # running in the background. Reuse the exact cross-process navigation
        # path used by the extension popup's Open button: persist a "navigate"
        # request for the UI-side CaptureWatcher to claim. The UI then posts
        # Events.NAVIGATE, which follows the normal show/raise/activate path and
        # gives Windows the same taskbar attention behavior as the popup Open
        # action.
        try:
            with self.context.session_factory() as session:
                BrowserRequestRepository(session).add(
                    "navigate",
                    payload={"page": "downloads"},
                )
            log.info(
                "player_download_navigation_queued",
                download_id=download.id,
            )
        except Exception:  # noqa: BLE001 - navigation feedback must not block download
            log.warning(
                "player_download_navigation_queue_failed",
                download_id=download.id,
                exc_info=True,
            )
        self._start_on_loop(download.id)
        return {
            "type": "download_detection_ok",
            "download_id": download.id,
            "filename": download.filename,
        }

    def _detection_file(
        self, entry: dict[str, Any], url: str, category: str, page_url: str
    ) -> dict[str, Any]:
        """Normalize one queued detection into a stored ``files_json`` entry.

        The capture context is *not* optional decoration: an m3u8/DASH manifest
        only downloads when the app can replay the request the player made. The
        streaming pipeline reads ``RequestContext`` back off the download row to
        recover the referer, the user agent and the player frame that can clear
        a bot-wall, so dropping these here is what makes stream downloads fail
        with no way to retry.
        """
        headers = {
            str(k): str(v)
            for k, v in (entry.get("headers") or {}).items()
            if v is not None and str(k).strip()
        }
        cookies = self._parse_cookies(entry.get("cookies"))
        referrer = str(entry.get("referrer") or "").strip()
        frame_url = str(entry.get("frame_url") or "").strip()
        if not frame_url and referrer:
            frame_url = referrer
        return {
            "url": url,
            "filename": str(entry.get("filename") or ""),
            "detected_type": str(entry.get("detected_type") or "other"),
            "category": category,
            "mime_type": str(entry.get("mime_type") or ""),
            "source": str(entry.get("source") or "network_capture"),
            # Cookies are stored as a mapping, never as the raw header string the
            # extension sends: the download manager merges them by name.
            "cookies": cookies,
            "headers": headers,
            "user_agent": str(entry.get("user_agent") or "").strip() or None,
            "referrer": referrer or None,
            "frame_url": frame_url or None,
            "data_base64": entry.get("data_base64") or None,
            "extension_id": str(entry.get("id") or ""),
        }

    def _open_detected(self, message: dict[str, Any]) -> dict[str, Any]:
        """Raise the app's Detected page, optionally filtered to a category.

        This runs in the native-host *process*, which shares the database with
        the app but not its event bus, so the navigation is queued as a request
        the UI polls instead of being posted as an in-process event.
        """
        from magnetoclip.ui.categories import normalize_detection_category

        category = normalize_detection_category(message.get("category"))
        try:
            with self.context.session_factory() as session:
                BrowserRequestRepository(session).add(
                    "navigate", payload={"page": "detected", "category": category}
                )
        except Exception:  # noqa: BLE001 - the caller still gets a usable answer
            log.warning("open_detected_queue_failed", exc_info=True)
            return {"type": "open_detected_error", "message": "could not open the Detected page"}
        log.info("open_detected_requested", category=category or "all")
        return {"type": "open_detected_ok", "category": category}

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

    @staticmethod
    def _is_media_capture(message: dict[str, Any]) -> bool:
        """True when a capture payload looks like image/video/audio content."""
        detected = str(message.get("detected_type") or "").strip().lower()
        if detected in ("image", "video", "audio"):
            return True
        mime = str(message.get("mime_type") or "").split(";")[0].strip().lower()
        if mime.startswith(("image/", "video/", "audio/")):
            return True
        for field in ("url", "filename"):
            root = str(message.get(field) or "").split("?", 1)[0].split("#", 1)[0]
            if re.search(
                r"\.(?:png|jpe?g|gif|webp|bmp|svg|tiff|ico|heic|avif|"
                r"mp4|webm|mkv|mov|avi|flv|m4v|3gp|ts|mpg|mpeg|"
                r"mp3|wav|ogg|oga|flac|m4a|aac|opus|wma|"
                r"m3u8|m3u|mpd)$",
                root,
                re.IGNORECASE,
            ):
                return True
        return False

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

    def _diag(self, message: dict[str, Any]) -> dict[str, Any]:
        """Persist an extension diagnostic for offline inspection.

        The extension reports what Telegram's server actually returned for live
        media requests (the page's own ``<video>`` requests and the live-capture
        probes) so the right capture strategy can be derived from evidence.
        """
        try:
            headers = message.get("headers")
            if isinstance(headers, dict):
                detail = " " + json.dumps(headers, sort_keys=True)
            else:
                detail = ""
            payload = (
                f"{message.get('kind') or ''}|{message.get('status') or ''}|"
                f"{message.get('url') or ''}|{message.get('range') or ''}{detail}"
            )
            with self.context.session_factory() as session:
                session.add(
                    BrowserEvent(
                        source="diag",
                        url=payload[:1500],
                        detected_type=str(message.get("source") or "extension_diag"),
                        ts=datetime.now(UTC),
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break capture
            log.warning("diag_record_failed", error=str(exc))
        return {"type": "ok"}

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

        client = build_client(
            ClientConfig(
                cookies=cookies or {},
                verify_tls=bool(self.context.settings.get("network.verify_tls", True)),
            )
        )
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
