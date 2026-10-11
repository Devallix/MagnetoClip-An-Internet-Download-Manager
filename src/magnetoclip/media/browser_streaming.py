"""Browser-assisted HLS downloads for Cloudflare-protected streams.

Streaming sites (anikai, hdtoday, ...) serve their video from CDNs behind
Cloudflare's "Attention Required" Managed Challenge. No HTTP library can pass
that challenge: Cloudflare fingerprints the real browser's TLS/HTTP2 stack,
cookie state and user-agent, so replayed fingerprints and cookies from Python
still get a ``403``.

The MagnetoClip browser extension runs in real Chrome and therefore clears the
challenge naturally. This module owns the app->extension native-messaging
transport (``BrowserFetcher``) plus the exceptions shared by the HLS pipeline:
one ``BrowserRequest`` row the extension serves back as chunked base64 is used
to fetch HLS manifests, keys and segments from the browser's own network stack.

The fetch transport is the same ``fetch_blob`` round-trip the UI uses for
pasted ``blob:`` URLs, but with ``background: True`` so the extension performs
the fetch from its service worker instead of requiring a tab for the CDN
origin (which never exists).

Download orchestration (concurrency, retry, resume, error classification)
lives in :mod:`magnetoclip.media.hls.downloader`, which this module re-exports
``download_browser_hls`` from so both the manager and the existing tests keep
their import paths. The parser symbols and ``_pick_stem``/``_decrypt_*``
helpers are re-exported here as backwards-compatible aliases.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time

import httpx
from pathlib import Path
from urllib.parse import unquote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..database.repositories import BrowserRequestRepository
from ..media.streaming import DownloadCancelled
from ..security.safe_names import sanitize_filename
from .hls.parser import (
    HlsPlaylist,
    HlsSegment,
    HlsVariant,
    parse_hls,
    select_variant,
)

log = logging.getLogger(__name__)

# Parser symbols stay importable from this module for backwards compatibility
# (the download engine moved to ``media/hls/downloader.py``).
__all__ = [
    "GENERIC_STEM",
    "_AUTO_FFMPEG",
    "BrowserFetchError",
    "BrowserFetcher",
    "BrowserManifestError",
    "BrowserUnavailableError",
    "HlsPlaylist",
    "HlsSegment",
    "HlsVariant",
    "_decrypt_aes128_cbc",
    "_default_iv",
    "_pick_stem",
    "download_browser_hls",
    "parse_hls",
    "select_variant",
]

GENERIC_STEM = {"index", "master", "playlist", "stream", "index-master", "m3u8"}

# Sentinel for "auto-detect ffmpeg", so callers can explicitly pass ``None`` to
# disable the ts->mp4 remux (tests) without losing the auto-detect behaviour.
_AUTO_FFMPEG = object()


class BrowserUnavailableError(Exception):
    """The browser/extension cannot serve the request.

    Raised before any bytes are written (master/variant resolution or a
    request that expired without the browser answering). Callers fall back to
    the yt-dlp path, which still works for non-Cloudflare manifest hosts.
    """

    def __init__(self, message, *, code=None, status=None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class BrowserFetchError(Exception):
    """The browser answered but could not deliver valid stream data.

    Raised mid-download (a segment/key fetch failed, or the stream needs
    unsupported encryption). Fatal: partial output is discarded.
    """

    def __init__(self, message, *, code=None, status=None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class BrowserManifestError(BrowserFetchError):
    """The browser could not resolve the manifests (master/variant/init).

    Raised before any bytes are written, so the caller can treat it like
    :class:`BrowserUnavailableError` and fall back to the yt-dlp path.
    """


class BrowserFetcher:
    """Fetch bytes through the extension's service worker.

    One ``BrowserRequest(type="fetch_blob", background=True)`` row per fetch;
    the native-messaging host pushes it to the extension, the extension streams
    the body back as ``blob_fetch_chunk`` and the host stores the assembled
    base64 on the same row. We poll until ready/error/timeout and always clean
    up rows we own.
    """

    def __init__(
        self,
        session_factory,
        *,
        referer: str | None = None,
        frame_url: str | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        timeout: float = 90.0,
        poll_interval: float = 0.25,
    ) -> None:
        self._session_factory = session_factory
        self._referer = referer
        self._frame_url = frame_url
        self._headers = dict(headers or {})
        self._cookies = dict(cookies or {})
        self._cancel_event = cancel_event
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._pending: list[int] = []
        self._closed = False

    def _native_fetch_bytes(self, url: str) -> bytes | None:
        """Replay a denied browser request with the captured HTTP context.

        Browser Fetch intentionally controls headers such as Origin and
        User-Agent, while some media CDNs validate those values. When the
        extension reports 401/403, replay the request natively with the
        capture-time headers/cookies. Only the exact request context supplied
        by the browser capture is used.
        """
        headers = {str(k): str(v) for k, v in self._headers.items() if v is not None}
        if self._cookies and not any(str(k).lower() == "cookie" for k in headers):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        if self._referer and not any(str(k).lower() == "referer" for k in headers):
            headers["Referer"] = self._referer

        timeout = httpx.Timeout(self._timeout)
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
            cookies=None,
        ) as client:
            response = client.get(url)
            if not response.is_success:
                raise httpx.HTTPStatusError(
                    f"native replay HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            return response.content

    def fetch_text(self, url: str) -> str:
        return self.fetch_bytes(url).decode("utf-8", errors="replace")

    def fetch_bytes(self, url: str) -> bytes:
        self._check_cancelled()
        payload: dict = {"url": url, "background": True}
        if self._referer:
            payload["referrer"] = self._referer
        if self._frame_url:
            payload["frame_url"] = self._frame_url
        if self._headers:
            payload["headers"] = self._headers
        with self._session_factory() as session:
            request = BrowserRequestRepository(session).add("fetch_blob", payload=payload)
        request_id = request.id
        self._pending.append(request_id)
        deadline = time.monotonic() + self._timeout
        log.info(
            "browser_fetch_start request_id=%s url=%s timeout=%.1f",
            request_id, url, self._timeout,
        )
        while True:
            self._check_cancelled()
            with self._session_factory() as session:
                row = BrowserRequestRepository(session).get(request_id)
            if row is None:
                self._expire(request_id)
                raise BrowserFetchError("the browser request was lost")
            if row.status == "error":
                message = str((row.result_json or {}).get("message") or "browser fetch failed")
                log.warning(
                    "browser_fetch_error request_id=%s url=%s error=%s",
                    request_id, url, message[:500],
                )
                if any(tag in message for tag in ("MagnetoClip is not reachable", "timed out")):
                    self._expire(request_id)
                    raise BrowserUnavailableError(message)
                status_match = re.search(r"HTTP\s+(\d{3})", message, re.IGNORECASE)
                status = int(status_match.group(1)) if status_match else None
                # Some CDNs require the exact page request context (notably
                # Origin + Referer + the browser's session cookie). A service
                # worker fetch cannot spoof browser-controlled Origin/User-Agent
                # headers, so retry 401/403 through the native HTTP client using
                # the capture-time request context. This is a replay of the
                # browser-observed request, not an access-control bypass.
                if status in (401, 403):
                    try:
                        direct = self._native_fetch_bytes(url)
                        if direct is not None:
                            self._discard(request_id)
                            return direct
                    except Exception as direct_exc:  # noqa: BLE001
                        log.debug(
                            "browser_native_context_fallback_failed url=%s error=%s",
                            url,
                            direct_exc,
                        )
                raise BrowserFetchError(message, status=status)
            if row.status == "ready":
                log.info(
                    "browser_fetch_ready request_id=%s url=%s bytes_b64=%d",
                    request_id, url, len(row.data_base64 or ""),
                )
                self._discard(request_id)
                if not row.data_base64:
                    raise BrowserFetchError("the browser returned empty content")
                try:
                    return base64.b64decode(row.data_base64, validate=True)
                except Exception:  # noqa: BLE001 - corrupt base64 is a transport failure
                    raise BrowserFetchError("the browser returned corrupt content")
            if row.status == "expired":
                self._discard(request_id)
                raise BrowserUnavailableError(
                    "the browser did not answer in time (is the extension connected?)"
                )
            if time.monotonic() >= deadline:
                log.error(
                    "browser_fetch_timeout request_id=%s url=%s timeout=%.1f",
                    request_id, url, self._timeout,
                )
                self._expire(request_id)
                raise BrowserUnavailableError(
                    f"timed out waiting for the browser to fetch {url}"
                )
            time.sleep(self._poll_interval)

    def close(self) -> None:
        """Expire any of our rows the browser never answered."""
        if self._closed:
            return
        self._closed = True
        for request_id in self._pending:
            self._expire(request_id)
        self._pending = []

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise DownloadCancelled()

    def _expire(self, request_id: int) -> None:
        try:
            with self._session_factory() as session:
                BrowserRequestRepository(session).mark_expired(request_id)
        except Exception:
            log.warning("browser_request_cleanup_failed id=%s", request_id, exc_info=True)

    def _discard(self, request_id: int) -> None:
        if request_id in self._pending:
            self._pending.remove(request_id)


def _decrypt_aes128_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(key) != 16:
        raise BrowserFetchError("invalid AES-128 key length")
    iv = bytes(iv)
    if len(iv) != 16:
        iv = iv[-16:] if len(iv) > 16 else iv.rjust(16, b"\x00")
    if len(iv) != 16:
        raise BrowserFetchError("invalid AES-128 IV")
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
    except Exception as exc:  # noqa: BLE001 - malformed ciphertext surfaces as an error
        raise BrowserFetchError(f"could not decrypt a segment: {exc}")
    if not padded:
        return b""
    pad = padded[-1]
    if 1 <= pad <= 16:
        padded = padded[:-pad]
    return bytes(padded)


def _default_iv(media_sequence: int, index: int) -> bytes:
    return (media_sequence + index).to_bytes(16, "big")


def _pick_stem(basename: str | None, referer_page: str | None) -> str:
    if basename:
        stem = Path(basename).stem.strip().lower() or basename.lower()
        if not any(g == stem for g in GENERIC_STEM) and not stem.endswith((".m3u8", ".m3u", ".mpd")):
            return sanitize_filename(Path(basename).stem.strip())
    if referer_page:
        page = referer_page.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        last = unquote(page.rsplit("/", 1)[-1])
        if last and not last.startswith("."):
            return sanitize_filename(last)
    return "stream"


def download_browser_hls(
    url: str,
    save_dir: Path,
    quality: str = "best",
    *,
    session_factory,
    referer: str | None = None,
    frame_url: str | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    basename: str | None = None,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    fetch_timeout: float = 90.0,
    poll_interval: float = 0.25,
    ffmpeg: str | None = _AUTO_FFMPEG,
    workers: int = 6,
    re_resolve_url=None,
) -> Path:
    """Convenience wrapper over the concurrent HLS engine.

    Imported locally to avoid an import cycle (the engine imports this module's
    transport). Raises :class:`BrowserUnavailableError` (fall back to yt-dlp),
    :class:`BrowserManifestError` (pre-download, fall back to yt-dlp) or
    :class:`BrowserFetchError` (fail the download).
    """
    from .hls.downloader import download_browser_hls as _concurrent_download

    return _concurrent_download(
        url,
        save_dir,
        quality,
        session_factory=session_factory,
        referer=referer,
        frame_url=frame_url,
        headers=headers,
        cookies=cookies,
        basename=basename,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        fetch_timeout=fetch_timeout,
        poll_interval=poll_interval,
        ffmpeg=ffmpeg,
        workers=workers,
        re_resolve_url=re_resolve_url,
    )
