"""Concurrent browser-assisted HLS download engine.

Sequentially downloading every segment over the app->extension native-messaging
round trip is slow (one ``fetch_blob`` BrowserRequest row at a time). This
engine replaces ``BrowserHlsDownloader`` with a pool of segment workers, each
owning its own :class:`BrowserFetcher` transport (so the extension's service
worker clears Cloudflare for every fetch without cross-thread state on a shared
fetcher).

Beyond pure concurrency it adds:

- a transient-failure retry policy with exponential backoff (5xx / 408 / 429);
- a stable error catalogue (:class:`StreamErrorCode`) attached to the
  transport exceptions it raises, so upper layers can react to expired media
  URLs, denied access, etc. without string matching;
- a resume sidecar (``<target>.part.resume.json``). The stored segment
  identities (query-agnostic URL paths) are validated against the freshly
  fetched media playlist and the download continues from the first un-written
  segment, so a paused download does not re-fetch its prefix;
- an optional ``re_resolve_url`` hook: when a signed manifest proves
  expired/denied (403), the caller can ask the extension to re-capture a fresh
  player context + manifest URL, and the engine retries the manifest fetch once.

Manifest-level failures still raise :class:`BrowserManifestError` so
``core/downloads/manager`` keeps its yt-dlp fall-back behaviour; mid-download
failures raise :class:`BrowserFetchError`. Both carry ``code``/``status``.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from ...security.safe_names import safe_join
from ..browser_streaming import (
    _AUTO_FFMPEG,
    BrowserFetcher,
    BrowserFetchError,
    BrowserManifestError,
    BrowserUnavailableError,
    _decrypt_aes128_cbc,
    _default_iv,
    _pick_stem,
)
from ..ffmpeg import _windowless_kwargs
from ..streaming import DownloadCancelled
from .parser import HlsPlaylist, HlsSegment, parse_hls, select_variant


class _CompatLogger:
    """Compatibility wrapper for legacy structured-log keyword calls.

    The HLS engine historically used calls such as ``log.info(..., target=...)``.
    ``logging.Logger`` only accepts a small set of keyword arguments, so those
    calls can otherwise abort an otherwise healthy download. Unknown fields are
    moved into ``extra``; standard logging keywords are passed through.
    """

    _STANDARD_KW = {"exc_info", "stack_info", "stacklevel", "extra"}

    def __init__(self, logger):
        self._logger = logger

    def _call(self, method, message, *args, **kwargs):
        extra = dict(kwargs.pop("extra", {}) or {})
        for key in list(kwargs):
            if key not in self._STANDARD_KW:
                extra[key] = kwargs.pop(key)
        if extra:
            kwargs["extra"] = extra
        return getattr(self._logger, method)(message, *args, **kwargs)

    def debug(self, message, *args, **kwargs):
        return self._call("debug", message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        return self._call("info", message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        return self._call("warning", message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        return self._call("error", message, *args, **kwargs)

    def exception(self, message, *args, **kwargs):
        return self._call("exception", message, *args, **kwargs)

    def critical(self, message, *args, **kwargs):
        return self._call("critical", message, *args, **kwargs)


log = _CompatLogger(logging.getLogger(__name__))

RESUME_EXT = ".resume.json"

# Worker pool size (segments in flight at once).
DEFAULT_WORKERS = 6

# Retry policy for transient segment/manifest failures.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# Transient HTTP statuses worth retrying; everything else fails immediately.
TRANSIENT_STATUSES = frozenset({408, 429}) | frozenset(range(500, 600))

# Query parameters that a signed/tokenized CDN URL (expiring media link) carries.
SIGNED_URL_PARAMS = frozenset({"token", "expires", "exp", "signature", "sig", "sign", "auth"})


@dataclass
class _ActiveDownload:
    """Shared state used to coalesce duplicate HLS download requests."""

    event: threading.Event
    result: Path | None = None
    error: BaseException | None = None


_ACTIVE_DOWNLOADS: dict[str, _ActiveDownload] = {}
_ACTIVE_DOWNLOADS_LOCK = threading.Lock()
_ACTIVE_OUTPUTS: set[str] = set()
_ACTIVE_OUTPUTS_LOCK = threading.Lock()


def _download_key(url: str, save_dir: Path, basename: str | None, quality: str) -> str:
    """Build a stable key for the same logical stream/output request.

    Signed query strings are deliberately ignored so repeated captures of the
    same tokenized HLS path coalesce while a new path remains independent.
    """
    parsed = urlparse(url)
    normalized = parsed._replace(query="", fragment="").geturl()
    return "\x1f".join((normalized, str(Path(save_dir).resolve()), basename or "", quality or "best"))


class StreamErrorCode(str, Enum):
    """Stable machine-readable failure codes, persisted on the download row."""

    NETWORK_ERROR = "NETWORK_ERROR"
    MEDIA_ACCESS_DENIED = "MEDIA_ACCESS_DENIED"
    PLAYLIST_UNAVAILABLE = "PLAYLIST_UNAVAILABLE"
    SEGMENT_ACCESS_DENIED = "SEGMENT_ACCESS_DENIED"
    EXPIRED_MEDIA_URL = "EXPIRED_MEDIA_URL"
    INVALID_PLAYLIST = "INVALID_PLAYLIST"
    UNSUPPORTED_STREAM = "UNSUPPORTED_STREAM"
    FFMPEG_ERROR = "FFMPEG_ERROR"


_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+?(\d{3})\b", re.IGNORECASE)


def _http_status(message: str) -> int | None:
    match = _HTTP_STATUS_RE.search(message or "")
    return int(match.group(1)) if match else None


def _status_transient(status: int | None) -> bool:
    return status is not None and status in TRANSIENT_STATUSES


def _signed_url(url: str) -> bool:
    query = parse_qs(urlparse(url).query)
    return bool(SIGNED_URL_PARAMS.intersection(query))


def _classify_manifest(url: str, message: str, status: int | None) -> StreamErrorCode:
    if status in (401, 403):
        return (
            StreamErrorCode.EXPIRED_MEDIA_URL
            if _signed_url(url)
            else StreamErrorCode.MEDIA_ACCESS_DENIED
        )
    if status in (404, 410, 451):
        return StreamErrorCode.PLAYLIST_UNAVAILABLE
    return StreamErrorCode.NETWORK_ERROR


def _classify_segment(url: str, status: int | None) -> StreamErrorCode:
    if status in (401, 403):
        return (
            StreamErrorCode.EXPIRED_MEDIA_URL
            if _signed_url(url)
            else StreamErrorCode.SEGMENT_ACCESS_DENIED
        )
    if status in (404, 410, 451):
        return StreamErrorCode.SEGMENT_ACCESS_DENIED
    return StreamErrorCode.NETWORK_ERROR


def _parse_iv(iv_hex: str | None, media_sequence: int, index: int) -> bytes:
    """Parse EXT-X-KEY IV safely; otherwise derive the RFC default IV."""
    if not iv_hex:
        return _default_iv(media_sequence, index)
    value = iv_hex.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    if not value:
        return _default_iv(media_sequence, index)
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise BrowserFetchError(
            f"invalid HLS encryption IV: {iv_hex}",
            code=StreamErrorCode.UNSUPPORTED_STREAM,
        ) from exc
    if len(raw) > 16:
        raise BrowserFetchError(
            "HLS encryption IV is longer than 128 bits",
            code=StreamErrorCode.UNSUPPORTED_STREAM,
        )
    return raw.rjust(16, b"\x00")


def _segment_signature(url: str) -> str:
    """Stable identity of a segment web request, ignoring the signed query."""
    return urlparse(url).path


def _looks_audio_only(playlist: HlsPlaylist) -> bool:
    ext = Path(playlist.segments[0].url).suffix.lower()
    return ext in {".aac", ".aacp", ".m4a", ".mp3"}


def _pick_container(playlist: HlsPlaylist, audio_only: bool) -> str:
    suffix = Path(playlist.segments[0].url).suffix.lower()
    if playlist.init_uri:
        # Fragmented MP4 (CMAF/HLS+fMP4): the init segment prefixes the output.
        return "mp4" if not audio_only else "m4a"
    if suffix in {".m4s", ".cmf"}:
        return "mp4" if not audio_only else "m4a"
    if audio_only:
        if suffix in {".mp3"}:
            return "mp3"
        if suffix in {".aac", ".aacp"}:
            # Raw ADTS AAC is only valid as a bare .aac without a remux.
            return "aac"
        return "m4a"
    if suffix in {".aac", ".aacp", ".m4a", ".mp3"}:
        return "mp4"
    return "ts"


def _estimate_total(bandwidth: int, duration: float) -> int:
    if bandwidth <= 0 or duration <= 0:
        return 0
    return int(bandwidth * duration / 8)


def _remux_ts(part: Path, target: Path, ffmpeg: str) -> Path:
    """Copy-remux a transport stream into target MP4 (no re-encode)."""
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(part), "-c", "copy", str(target)],
            capture_output=True,
            check=False,
            timeout=300,
            **_windowless_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserFetchError(
            f"ffmpeg remux failed: {exc}", code=StreamErrorCode.FFMPEG_ERROR
        )
    stderr = (result.stderr or b"").decode(errors="replace").strip()
    stdout = (result.stdout or b"").decode(errors="replace").strip()
    log.info(
        "hls_ffmpeg_result returncode=%s target=%s stderr=%s stdout=%s",
        result.returncode,
        target,
        stderr[-1200:] if stderr else "",
        stdout[-400:] if stdout else "",
    )
    if result.returncode != 0 or not target.exists():
        raise BrowserFetchError(
            f"ffmpeg remux failed: {stderr[:800] or 'unknown error'}",
            code=StreamErrorCode.FFMPEG_ERROR,
        )
    return target


class _SegmentJob:
    __slots__ = ("index", "iv", "key", "url")

    def __init__(self, index: int, url: str, key: bytes | None, iv: bytes) -> None:
        self.index = index
        self.url = url
        self.key = key
        self.iv = iv


class _SegmentWorker(threading.Thread):
    """Fetches + decrypts segments from the browser's network stack.

    Owns a private :class:`BrowserFetcher` (no shared pending-list across
    threads) that it always closes. Delivers ``(index, bytes, None)`` or
    ``(index, None, exc)`` through the ``results`` queue; a failed job sets the
    abort flag so sibling workers stop pulling new work.
    """

    def __init__(
        self,
        jobs: queue.Queue[_SegmentJob | None],
        results: queue.Queue,
        abort: threading.Event,
        make_fetcher: Callable[[], BrowserFetcher],
    ) -> None:
        super().__init__(daemon=True)
        self._jobs = jobs
        self._results = results
        self._abort = abort
        self._make_fetcher = make_fetcher

    def run(self) -> None:
        fetcher = self._make_fetcher()
        try:
            while True:
                if self._abort.is_set():
                    return
                try:
                    job = self._jobs.get(timeout=0.25)
                except queue.Empty:
                    continue
                if job is None:
                    return
                if not self._fetch_with_retries(fetcher, job):
                    return
        finally:
            fetcher.close()

    def _fetch_with_retries(self, fetcher: BrowserFetcher, job: _SegmentJob) -> bool:
        started = time.monotonic()
        log.info("hls_segment_request_start index=%d url=%s", job.index, job.url)
        try:
            data = fetcher.fetch_bytes(job.url)
        except DownloadCancelled as exc:
            log.info(
                "hls_segment_request_cancelled index=%d elapsed=%.2f",
                job.index, time.monotonic() - started,
            )
            self._abort.set()
            self._results.put((job.index, None, exc))
            return False
        except (BrowserFetchError, BrowserUnavailableError) as exc:
            log.warning(
                "hls_segment_request_error index=%d elapsed=%.2f error=%s",
                job.index, time.monotonic() - started, str(exc)[:500],
            )
            return self._retry(fetcher, job, exc)
        log.info(
            "hls_segment_request_response index=%d bytes=%d elapsed=%.2f",
            job.index, len(data), time.monotonic() - started,
        )
        return self._deliver(fetcher, job, data)

    def _deliver(self, fetcher: BrowserFetcher, job: _SegmentJob, data: bytes) -> bool:
        try:
            if job.key is not None:
                data = _decrypt_aes128_cbc(data, job.key, job.iv)
        except BrowserFetchError as exc:
            self._fail(job, exc)
            return False
        self._results.put((job.index, data, None))
        return True

    def _fail(self, job: _SegmentJob, exc: Exception) -> None:
        """Report a terminal segment failure, stamping its error catalogue."""
        if not isinstance(exc, DownloadCancelled):
            status = _http_status(str(exc))
            if getattr(exc, "code", None) is None:
                exc.code = _classify_segment(job.url, status)
            if getattr(exc, "status", None) is None:
                exc.status = status
        self._abort.set()
        self._results.put((job.index, None, exc))

    def _retry(self, fetcher: BrowserFetcher, job: _SegmentJob, first: Exception) -> bool:
        """Retry a transient failure with backoff; else fail the download."""
        last = first
        for attempt in range(1, MAX_ATTEMPTS):
            status = _http_status(str(last))
            if not _status_transient(status):
                self._fail(job, last)
                return False
            time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
            retry_started = time.monotonic()
            log.info(
                "hls_segment_retry_start index=%d attempt=%d url=%s",
                job.index, attempt + 1, job.url,
            )
            try:
                data = fetcher.fetch_bytes(job.url)
            except DownloadCancelled as exc:
                self._fail(job, exc)
                return False
            except (BrowserFetchError, BrowserUnavailableError) as exc:
                log.warning(
                    "hls_segment_retry_error index=%d attempt=%d elapsed=%.2f error=%s",
                    job.index, attempt + 1, time.monotonic() - retry_started, str(exc)[:500],
                )
                last = exc
                continue
            log.info(
                "hls_segment_retry_response index=%d attempt=%d bytes=%d elapsed=%.2f",
                job.index, attempt + 1, len(data), time.monotonic() - retry_started,
            )
            if job.key is not None:
                try:
                    data = _decrypt_aes128_cbc(data, job.key, job.iv)
                except BrowserFetchError as exc:
                    self._fail(job, exc)
                    return False
            self._results.put((job.index, data, None))
            return True
        self._fail(job, last)
        return False


class HlsDownloader:
    """Orchestrates a browser-assisted HLS download and assembles the file."""

    def __init__(
        self,
        session_factory,
        *,
        referer: str | None = None,
        frame_url: str | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        fetch_timeout: float = 90.0,
        poll_interval: float = 0.25,
        ffmpeg: str | None = None,
        workers: int = DEFAULT_WORKERS,
        re_resolve_url: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._referer = referer
        self._cancel_event = cancel_event
        self._fetch_timeout = fetch_timeout
        self._poll_interval = poll_interval
        self._ffmpeg = ffmpeg
        self._workers = max(1, int(workers))
        self._re_resolve_url = re_resolve_url
        self._fresh_used = False
        self._fresh_url: str | None = None
        self._active_referer = referer
        self._frame_url = frame_url
        self._headers = dict(headers or {})
        self._cookies = dict(cookies or {})
        self._fetcher = self._make_fetcher()
        self._part: Path | None = None
        self._reserved_output_key: str | None = None

    def download(
        self,
        url: str,
        save_dir: Path,
        quality: str = "best",
        *,
        basename: str | None = None,
        referer_page: str | None = None,
        progress_cb=None,
    ) -> Path:
        """Run the download; returns the final file path.

        A :class:`DownloadCancelled` preserves the partial output + resume
        sidecar so a later run can continue; every other failure discards them
        (matching pre-resume semantics).
        """
        key = _download_key(url, save_dir, basename, quality)
        owner = False
        with _ACTIVE_DOWNLOADS_LOCK:
            active = _ACTIVE_DOWNLOADS.get(key)
            if active is None:
                active = _ActiveDownload(event=threading.Event())
                _ACTIVE_DOWNLOADS[key] = active
                owner = True

        if not owner:
            log.info(
                "hls_duplicate_stream_waiting url=%s save_dir=%s quality=%s",
                url, str(save_dir), quality,
            )
            active.event.wait()
            if active.error is not None:
                raise active.error
            if active.result is None:
                raise BrowserFetchError(
                    "duplicate HLS download completed without an output path",
                    code=StreamErrorCode.NETWORK_ERROR,
                )
            log.info("hls_duplicate_stream_reused url=%s path=%s", url, active.result)
            return active.result

        try:
            result = self._run(url, save_dir, quality, basename, referer_page, progress_cb)
            active.result = result
            return result
        except DownloadCancelled as exc:
            active.error = exc
            raise  # keep part + sidecar for a later resume
        except Exception as exc:
            active.error = exc
            # Close the primary browser transport before touching the temporary
            # output. On Windows, an outstanding transport/file handle can make
            # unlink() fail with WinError 32.
            try:
                self._fetcher.close()
            except Exception:
                log.debug("hls_primary_fetcher_close_failed", exc_info=True)
            if self._part is not None:
                part = self._part
                self._part = None
                self._safe_unlink(part)
                self._safe_unlink(self._resume_sidecar(part))
            raise
        finally:
            try:
                self._fetcher.close()
            except Exception:
                log.debug("hls_primary_fetcher_close_failed", exc_info=True)
            if self._reserved_output_key is not None:
                with _ACTIVE_OUTPUTS_LOCK:
                    _ACTIVE_OUTPUTS.discard(self._reserved_output_key)
                self._reserved_output_key = None
            with _ACTIVE_DOWNLOADS_LOCK:
                current = _ACTIVE_DOWNLOADS.get(key)
                if current is active:
                    _ACTIVE_DOWNLOADS.pop(key, None)
                active.event.set()

    @staticmethod
    def _safe_unlink(path: Path, attempts: int = 5, delay: float = 0.25) -> None:
        """Best-effort Windows-safe cleanup for temporary HLS files."""
        for attempt in range(attempts):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt + 1 >= attempts:
                    log.warning(
                        "hls_temp_cleanup_failed path=%s",
                        path,
                        exc_info=True,
                    )
                    return
                time.sleep(delay)
            except OSError:
                log.debug("hls_temp_cleanup_failed path=%s", path, exc_info=True)
                return

    def _make_fetcher(self) -> BrowserFetcher:
        return BrowserFetcher(
            self._session_factory,
            referer=self._active_referer,
            frame_url=self._frame_url,
            headers=self._headers,
            cookies=self._cookies,
            cancel_event=self._cancel_event,
            timeout=self._fetch_timeout,
            poll_interval=self._poll_interval,
        )

    @staticmethod
    def _resume_sidecar(part: Path) -> Path:
        return part.with_name(f"{part.name}{RESUME_EXT}")

    def _run(
        self,
        url: str,
        save_dir: Path,
        quality: str,
        basename: str | None,
        referer_page: str | None,
        progress_cb,
    ) -> Path:
        try:
            master_text = self._read_manifest_text(self._fetcher, url, on_master=True)
        except BrowserManifestError as exc:
            if not self._try_fresh_manifest(url, exc):
                raise
            self._fetcher.close()
            self._fetcher = self._make_fetcher()
            url = str(self._fresh_url)
            master_text = self._read_manifest_text(self._fetcher, url, on_master=True)
        if "#EXT" not in master_text:
            # Cloudflare sometimes answers a Managed Challenge page with HTTP 200
            # and an HTML body instead of an m3u8. Any real HLS playlist carries
            # an ``#EXT...`` tag, so its absence means this is not HLS at all.
            raise BrowserManifestError(
                "the browser returned a non-HLS response (likely a Cloudflare challenge page)",
                code=StreamErrorCode.INVALID_PLAYLIST,
            )
        master = parse_hls(master_text, url)
        log.info(
            "hls_master_parsed url=%s variants=%d renditions=%d is_master=%s",
            url, len(master.variants), len(master.renditions), master.is_master,
        )

        if master.is_master:
            if not master.variants:
                raise BrowserFetchError(
                    "the master playlist has no variants",
                    code=StreamErrorCode.INVALID_PLAYLIST,
                )
            variant = select_variant(master.variants, quality)
            log.info(
                "hls_variant_selected url=%s quality=%s height=%s bandwidth=%s audio_group=%s audio_url=%s",
                variant.url, quality, variant.height, variant.bandwidth,
                variant.audio_group, variant.audio_url,
            )
            media_text = self._read_manifest_text(self._fetcher, variant.url, on_master=False)
            playlist = parse_hls(media_text, variant.url)
            log.info(
                "hls_media_playlist_parsed url=%s segments=%d duration=%s",
                variant.url, len(playlist.segments), playlist.total_duration,
            )
            bandwidth = variant.bandwidth or master.bandwidth
        else:
            playlist = master
            bandwidth = master.bandwidth

        if playlist.is_master:
            # A nested master (rare) -- resolve again one level down.
            variant = select_variant(playlist.variants, quality)
            playlist = parse_hls(
                self._read_manifest_text(self._fetcher, variant.url, on_master=False),
                variant.url,
            )
            bandwidth = variant.bandwidth or bandwidth

        if not playlist.segments:
            raise BrowserManifestError(
                "the browser delivered a playlist with no segments",
                code=StreamErrorCode.INVALID_PLAYLIST,
            )

        audio_only = quality == "audio" or _looks_audio_only(playlist)
        container = _pick_container(playlist, audio_only)
        ext = container
        if ext == "ts" and not audio_only and self._ffmpeg is not None:
            ext = "mp4"

        stem = _pick_stem(basename, referer_page)
        target = safe_join(save_dir, f"{stem}.{ext}")

        # Different captures can legitimately point at the same stream title
        # while using different signed manifest URLs (for example a master and
        # an index playlist captured from the same player). They therefore do
        # not necessarily hit the URL-level duplicate coalescer above. On
        # Windows, letting both workers use the same .part path causes handle
        # contention and can corrupt/resume the wrong job. Reserve the output
        # path explicitly and give later concurrent captures a deterministic
        # unique filename. Completed files are left untouched; this only
        # disambiguates paths currently owned by another active HLS download.
        target_key = str(target.resolve()).casefold()
        with _ACTIVE_OUTPUTS_LOCK:
            if target_key in _ACTIVE_OUTPUTS:
                base_stem = target.stem
                suffix = target.suffix
                counter = 2
                while True:
                    candidate = target.with_name(f"{base_stem} ({counter}){suffix}")
                    candidate_key = str(candidate.resolve()).casefold()
                    if candidate_key not in _ACTIVE_OUTPUTS:
                        target = candidate
                        target_key = candidate_key
                        log.info(
                            "hls_output_collision_avoided original=%s target=%s",
                            target.with_name(f"{base_stem}{suffix}"),
                            target,
                        )
                        break
                    counter += 1
            _ACTIVE_OUTPUTS.add(target_key)
        self._reserved_output_key = target_key

        part = target.with_name(f"{target.name}.part")
        # A stale .part file is a valid resume candidate. If it cannot be used
        # because its sidecar is malformed, the existing code restarts it. The
        # important invariant is that two *active* requests for the same stream
        # never share this file; duplicate active requests are coalesced by
        # download().
        log.info("hls_output_reserved target=%s part=%s", target, part)
        self._part = part

        total_duration = playlist.total_duration
        total_estimate = _estimate_total(bandwidth, total_duration) if total_duration else 0

        start_index, append = 0, False
        if part.exists():
            resume_from = self._resume_window(playlist, part)
            if resume_from is not None:
                start_index, append = resume_from, True
            else:
                # A stale .part can still be held open by a previous failed
                # worker/browser request on Windows. Never let cleanup turn
                # that recoverable condition into a new download failure.
                # Close our browser transport first, then retry cleanup; if
                # another process still owns the handle, continue with a
                # unique temporary file instead.
                try:
                    self._fetcher.close()
                except Exception:
                    log.debug("hls_pre_cleanup_fetcher_close_failed", exc_info=True)
                self._safe_unlink(part)
                self._safe_unlink(self._resume_sidecar(part))
                if part.exists():
                    old_part = part
                    part = target.with_name(
                        f"{target.stem}.{uuid4().hex[:8]}{target.suffix}.part"
                    )
                    self._part = part
                    log.warning(
                        "hls_locked_stale_part_using_unique_temp old_part=%s part=%s",
                        old_part, part,
                    )

        key_cache = self._prefetch_keys(playlist.segments)
        jobs = self._build_jobs(playlist, key_cache, start_index)
        log.info(
            "hls_segments_ready url=%s total=%d start_index=%d jobs=%d duration=%s init=%s",
            url, len(playlist.segments), start_index, len(jobs),
            playlist.total_duration, bool(playlist.init_uri),
        )
        for preview_index, preview_job in enumerate(jobs[:3]):
            log.info(
                "hls_segment_preview index=%d url=%s",
                preview_job.index, preview_job.url,
            )

        self._assemble(playlist, part, jobs, append, total_estimate, progress_cb)

        final = None
        if ext == "mp4" and container == "ts":
            final = _remux_ts(part, target, self._ffmpeg)
            part.unlink(missing_ok=True)
        else:
            part.rename(target)
            final = target
        self._resume_sidecar(part).unlink(missing_ok=True)
        self._part = None
        return final

    def _read_manifest_text(self, fetcher: BrowserFetcher, url: str, *, on_master: bool) -> str:
        data = self._fetch_with_retries(fetcher, url, on_master=on_master)
        text = data.decode("utf-8", errors="replace")
        if "#EXT" not in text:
            raise BrowserManifestError(
                "the browser returned a non-HLS response for a playlist",
                code=StreamErrorCode.INVALID_PLAYLIST,
            )
        return text

    def _fetch_with_retries(
        self, fetcher: BrowserFetcher, url: str, *, on_master: bool
    ) -> bytes:
        prefix = (
            "could not read the master playlist"
            if on_master
            else "could not read a stream manifest from the browser"
        )
        last_exc: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            log.info(
                "hls_manifest_request url=%s kind=%s attempt=%d signed=%s",
                url,
                "master" if on_master else "media",
                attempt + 1,
                _signed_url(url),
            )
            try:
                data = fetcher.fetch_bytes(url)
                log.info(
                    "hls_manifest_response url=%s kind=%s bytes=%d",
                    url,
                    "master" if on_master else "media",
                    len(data),
                )
                return data
            except DownloadCancelled:
                raise
            except (BrowserUnavailableError, BrowserFetchError) as exc:
                last_exc = exc
                status = _http_status(str(exc))
                if _status_transient(status) and attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                    continue
                raise BrowserManifestError(
                    f"{prefix}: {exc}",
                    code=_classify_manifest(url, str(exc), status),
                    status=status,
                ) from exc
        assert last_exc is not None  # pragma: no cover - loop returns or raises
        raise BrowserManifestError(f"{prefix}: {last_exc}") from last_exc

    def _try_fresh_manifest(self, url: str, exc: BrowserManifestError) -> bool:
        """Ask the caller to re-capture a fresh manifest/player context."""
        if self._re_resolve_url is None or self._fresh_used:
            return False
        if exc.code not in {
            StreamErrorCode.EXPIRED_MEDIA_URL,
            StreamErrorCode.MEDIA_ACCESS_DENIED,
        } or exc.status != 403:
            return False
        try:
            result = self._re_resolve_url(url)
        except Exception:
            log.warning("hls_fresh_manifest_hook_failed url=%s", url, exc_info=True)
            return False
        if not result or not result[0]:
            return False
        self._fresh_used = True
        self._fresh_url, new_referer = result[0], result[1]
        if new_referer:
            self._active_referer = new_referer
        return True

    def _resume_window(self, playlist: HlsPlaylist, part: Path) -> int | None:
        """Return the segment index to resume from, or ``None`` to restart.

        The stored identities (query-agnostic URL paths) must equal the freshly
        parsed playlist's prefix, so a re-signed manifest resumes only when its
        relative layout is unchanged.
        """
        sidecar = self._resume_sidecar(part)
        try:
            meta = json.loads(sidecar.read_text())
        except (OSError, ValueError):
            return None
        stored = meta.get("paths") if isinstance(meta, dict) else None
        if not isinstance(stored, list) or not stored:
            return None
        if len(stored) > len(playlist.segments):
            return None
        for explored, segment in enumerate(playlist.segments[: len(stored)]):
            if _segment_signature(segment.url) != stored[explored]:
                return None
        return len(stored)

    def _build_jobs(
        self, playlist: HlsPlaylist, key_cache: dict[str, bytes], start_index: int
    ) -> list[_SegmentJob]:
        jobs: list[_SegmentJob] = []
        media_sequence = playlist.media_sequence
        for index, segment in enumerate(playlist.segments):
            if index < start_index:
                continue
            if segment.key_uri:
                if segment.key_method.upper() != "AES-128":
                    raise BrowserFetchError(
                        f"unsupported HLS encryption method: {segment.key_method}",
                        code=StreamErrorCode.UNSUPPORTED_STREAM,
                    )
                key = key_cache.get(segment.key_uri)
                iv = _parse_iv(segment.iv_hex, media_sequence, index)
            else:
                key, iv = None, b""
            jobs.append(_SegmentJob(index, segment.url, key, iv))
        return jobs

    def _prefetch_keys(self, segments: list[HlsSegment]) -> dict[str, bytes]:
        """Fetch unique AES-128 keys serially before spawning any workers.

        Keys are rare (typically zero or one) and a key failure should abort
        before any segment bytes move.
        """
        cache: dict[str, bytes] = {}
        for segment in segments:
            if not segment.key_uri or segment.key_uri in cache:
                continue
            if segment.key_method.upper() != "AES-128":
                raise BrowserFetchError(
                    f"unsupported HLS encryption method: {segment.key_method}",
                    code=StreamErrorCode.UNSUPPORTED_STREAM,
                )
            try:
                key_bytes = self._fetch_with_retries(
                    self._fetcher, segment.key_uri, on_master=False
                )
            except BrowserManifestError as exc:
                raise BrowserFetchError(
                    f"could not fetch the AES-128 key: {exc}", code=exc.code
                )
            if len(key_bytes) != 16:
                raise BrowserFetchError(
                    "invalid AES-128 key size",
                    code=StreamErrorCode.UNSUPPORTED_STREAM,
                )
            cache[segment.key_uri] = key_bytes
        return cache

    def _assemble(
        self,
        playlist: HlsPlaylist,
        part: Path,
        jobs: list[_SegmentJob],
        append: bool,
        total_estimate: int,
        progress_cb,
    ) -> None:
        if not jobs:
            log.warning("hls_assemble_no_jobs")
            return
        log.info(
            "hls_assemble_start jobs=%d workers=%d append=%s init=%s",
            len(jobs), min(self._workers, len(jobs)), append, bool(playlist.init_uri),
        )
        init_prefix = b""
        if playlist.init_uri:
            log.info("hls_init_segment_request url=%s", playlist.init_uri)
            init_prefix = self._fetch_with_retries(
                self._fetcher, playlist.init_uri, on_master=False
            )

        job_queue: queue.Queue = queue.Queue()
        for job in jobs:
            job_queue.put(job)

        results: queue.Queue = queue.Queue(maxsize=self._workers * 2)
        abort = threading.Event()
        workers = [
            _SegmentWorker(job_queue, results, abort, self._make_fetcher)
            for _ in range(min(self._workers, len(jobs)))
        ]
        for worker in workers:
            worker.start()

        try:
            self._write_segments(
                part,
                playlist.segments,
                jobs,
                results,
                init_prefix,
                append,
                total_estimate,
                progress_cb,
            )
        finally:
            abort.set()
            for _ in workers:
                job_queue.put(None)  # one sentinel per worker so none stays blocked
            for worker in workers:
                worker.join(timeout=2.0)

    def _write_segments(
        self,
        part: Path,
        segments: list[HlsSegment],
        jobs: list[_SegmentJob],
        results: queue.Queue,
        init_prefix: bytes,
        append: bool,
        total_estimate: int,
        progress_cb,
    ) -> None:
        start = jobs[0].index
        end = start + len(jobs)
        held: dict[int, bytes] = {}
        downloaded = 0
        mode = "ab" if append else "wb"
        with part.open(mode) as handle:
            if not append and init_prefix:
                handle.write(init_prefix)
                downloaded += len(init_prefix)
            done = start
            try:
                while done < end:
                    self._check_cancelled()
                    index, data, exc = results.get()
                    if exc is not None:
                        raise exc
                    if index == done:
                        handle.write(data)
                        downloaded += len(data)
                        self._report_progress(
                            downloaded, total_estimate, progress_cb, index, len(jobs)
                        )
                        done += 1
                        while done in held:
                            payload = held.pop(done)
                            handle.write(payload)
                            downloaded += len(payload)
                            self._report_progress(
                                downloaded, total_estimate, progress_cb, done, len(jobs)
                            )
                            done += 1
                        handle.flush()
                    else:
                        # Workers pull in FIFO order, so out-of-order completions
                        # are bounded; the results queue's maxsize back-pressures.
                        held[index] = data
            finally:
                # Persist on success *and* on cancellation so a paused download
                # can resume from the last contiguously written segment.
                self._persist_resume(part, segments, done)

    def _persist_resume(self, part: Path, segments: list[HlsSegment], written: int) -> None:
        paths = [_segment_signature(segment.url) for segment in segments[:written]]
        try:
            self._resume_sidecar(part).write_text(
                json.dumps({"paths": paths}), encoding="utf-8"
            )
        except OSError:
            log.warning("hls_resume_sidecar_write_failed part=%s", part, exc_info=True)

    def _report_progress(self, downloaded: int, total_estimate: int, progress_cb, index: int, count: int) -> None:
        if progress_cb:
            try:
                progress_cb(downloaded, total_estimate)
            except Exception:
                # UI/progress reporting must never abort an otherwise healthy
                # media transfer.
                log.warning(
                    "hls_progress_callback_failed index=%d segments=%d bytes=%d",
                    index,
                    count,
                    downloaded,
                    exc_info=True,
                )
        log.debug("hls_segment index=%d segments=%d bytes=%d", index, count, downloaded)

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise DownloadCancelled()


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
    workers: int = DEFAULT_WORKERS,
    re_resolve_url: Callable[[str], tuple[str, str] | None] | None = None,
) -> Path:
    """Download an HLS stream through the browser transport.

    Raises :class:`BrowserUnavailableError` (fall back to yt-dlp),
    :class:`BrowserManifestError` (pre-download, fall back to yt-dlp) or
    :class:`BrowserFetchError` (fail the download).
    """
    if ffmpeg is _AUTO_FFMPEG:
        ffmpeg = shutil.which("ffmpeg") or None
    downloader = HlsDownloader(
        session_factory,
        referer=referer,
        frame_url=frame_url,
        headers=headers,
        cookies=cookies,
        cancel_event=cancel_event,
        fetch_timeout=fetch_timeout,
        poll_interval=poll_interval,
        ffmpeg=ffmpeg,
        workers=workers,
        re_resolve_url=re_resolve_url,
    )
    return downloader.download(
        url,
        save_dir,
        quality,
        basename=basename,
        referer_page=referer,
        progress_cb=progress_cb,
    )