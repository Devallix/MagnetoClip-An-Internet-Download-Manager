"""MagnetoCore: orchestration of single and multi-connection downloads.

Public surface:
- :func:`analyze` — probe a remote resource.
- :class:`DownloadTask` — runs one download to completion (or failure).
- :class:`MagnetoCore` — owns the shared client/limiter and active tasks.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from magnetoclip.core.events.bus import Events
from magnetoclip.intelligence import BandwidthAllocator
from magnetoclip.network.auth.credentials import AuthSpec
from magnetoclip.network.content import should_reject_html_body
from magnetoclip.network.http.client import ClientConfig, build_client
from magnetoclip.network.http.disposition import parse_content_disposition
from magnetoclip.network.http.range import parse_content_range
from magnetoclip.network.proxy.profiles import ProxySpec
from magnetoclip.security.safe_names import sanitize_filename
from magnetoclip.services.logging.setup import get_logger

from ..resume.mclip import MClipState, SegmentState, reconcile_part_sizes
from ..retry.policy import (
    PermanentError,
    RangeNotSupported,
    SegmentAborted,
    classify_http_status,
)
from ..segmenter.planner import plan_segments
from ..verification.hasher import hash_file_async, verify
from .pool import SegmentPool
from .throttle import RateLimiter, ThroughputMonitor
from .types import DownloadSpec

log = get_logger(__name__)

TERMINAL_STATES = ("completed", "failed", "verification_failed", "stopped")


@dataclass
class RemoteFileInfo:
    total_size: int | None = None
    supports_ranges: bool = True
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    content_disposition_filename: str | None = None


async def analyze(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> RemoteFileInfo:
    """Probe a remote resource for size, range support, and metadata."""
    request_headers = {**(headers or {}), "Range": "bytes=0-0"}
    timeout_value = httpx.Timeout(timeout)
    # Headers only — never iterate the body. Breaking out of aiter_bytes()
    # leaves a suspended async generator behind whose GC finalization runs
    # in a foreign task and trips anyio/httpcore ("async generator ignored
    # GeneratorExit" / "cancel scope in a different task").
    async with client.stream(
        "GET", url, headers=request_headers, timeout=timeout_value
    ) as response:
        if response.status_code not in (200, 206):
            exc = classify_http_status(response.status_code)
            raise exc(f"HTTP {response.status_code}") from httpx.HTTPStatusError(
                f"HTTP {response.status_code}",
                request=response.request,
                response=response,
            )

        content_range = response.headers.get("Content-Range")
        total_size: int | None = None
        supports_ranges = response.status_code == 206
        if content_range:
            _, _, total = parse_content_range(content_range)
            total_size = total
        if total_size is None:
            length = response.headers.get("Content-Length")
            if length:
                total_size = int(length)

        return RemoteFileInfo(
            total_size=total_size,
            supports_ranges=supports_ranges,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            content_type=response.headers.get("Content-Type"),
            content_disposition_filename=parse_content_disposition(
                response.headers.get("Content-Disposition")
            ),
        )


class DownloadTask:
    """Runs a single download through its full lifecycle."""

    def __init__(
        self,
        *,
        spec: DownloadSpec,
        state: MClipState | None = None,
        client: httpx.AsyncClient,
        limiter: RateLimiter | None = None,
        bus=None,
        report_interval: float = 0.5,
        persist_interval: float = 1.0,
        close_client: bool = False,
    ) -> None:
        self.spec = spec
        self.state = state or MClipState(
            url=spec.url,
            file_path=str(spec.final_path),
            headers=dict(spec.headers),
            hash_algo=spec.hash_algo,
            hash_expected=spec.hash_expected,
        )
        self.client = client
        self.limiter = limiter
        self.bus = bus
        self.report_interval = report_interval
        self.persist_interval = persist_interval
        self.close_client = close_client

        self._running = asyncio.Event()
        self._running.set()
        self._cancel = asyncio.Event()
        self._done = asyncio.Event()
        self._monitor = ThroughputMonitor()
        self._last_persist = 0.0
        self._active_connections = 0

        self.result_state: str | None = None
        self.error: str | None = None

    @property
    def done(self) -> asyncio.Event:
        return self._done

    def is_terminal(self) -> bool:
        return self.result_state in TERMINAL_STATES

    # ----- control -----

    def pause(self) -> None:
        if self.state.state in ("downloading", "connecting", "retrying"):
            self._running.clear()
            self._set_state("paused")

    def resume(self) -> None:
        if self.state.state == "paused":
            self._running.set()
            self._set_state("downloading")
        elif not self._running.is_set():
            # A pause landed during the brief connecting→downloading
            # transition: the state already reads "downloading" but the
            # workers are still blocked, so just release them.
            self._running.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._running.set()

    def set_bandwidth(self, bytes_per_second: float) -> None:
        if self.limiter is not None:
            self.limiter.set_rate(bytes_per_second)

    # ----- execution -----

    async def run(self) -> str:
        if self.state.state == "completed":
            self.result_state = "completed"
            self._done.set()
            return self.result_state

        report_task = asyncio.create_task(self._report_loop())
        try:
            try:
                await self._run_inner()
            except RangeNotSupported:
                await self._restart_single_segment()
            except SegmentAborted:
                if self._cancel.is_set():
                    self._set_state("stopped")
                # else: paused — the task stays alive for resume
            except PermanentError as exc:
                log.warning(
                    "download_permanent_failed",
                    download_id=self.spec.download_id,
                    error=str(exc),
                )
                self._fail(str(exc))
            except Exception as exc:  # noqa: BLE001 - engine must not crash
                log.exception("download_failed", download_id=self.spec.download_id)
                self._fail(str(exc))
        finally:
            self._persist()
            report_task.cancel()
            await asyncio.gather(report_task, return_exceptions=True)
            if self.close_client:
                await self.client.aclose()
            self._done.set()
        return self.result_state

    async def _run_inner(self) -> None:
        if self.state.total_size is None or self.state.state == "queued":
            self._set_state("connecting")
            info = await self._analyze_with_retry()
            self.state.total_size = info.total_size
            self.state.etag = info.etag or self.state.etag
            self.state.last_modified = info.last_modified or self.state.last_modified
            if not info.supports_ranges:
                self.spec.connections_max = 1
            self._reject_html_substitute(info)

        if not self.state.segments:
            self._plan_segments()
        reconcile_part_sizes(self.state)
        self._reconcile_segments_for_size()

        self._set_state("downloading")
        if not self._running.is_set():
            # Paused while the resource was being analysed: pause() cleared the
            # running event, but the _set_state("downloading") above would have
            # overwritten the "paused" state that resume() relies on.
            self._set_state("paused")
        self._persist()

        pool = SegmentPool(
            self.state.segments,
            self.spec,
            client=self.client,
            limiter=self.limiter,
            running=self._running,
            cancel=self._cancel,
            monitor=self._monitor,
            on_segment_done=self._on_segment_done,
        )
        await pool.run()
        self._active_connections = 0

        await asyncio.to_thread(self._merge_parts)
        if self.spec.hash_algo and self.spec.hash_expected:
            self._set_state("verifying")
            self._persist()
            calculated = await hash_file_async(self.spec.final_path, self.spec.hash_algo)
            self.state.hash_calculated = calculated
            if verify(self.spec.hash_expected, calculated):
                self._finish()
            else:
                self.state.state = "verification_failed"
                self.result_state = "verification_failed"
                self.error = "integrity check failed"
                self._emit(Events.DOWNLOAD_STATE_CHANGED, {
                    "id": self.spec.download_id,
                    "state": "verification_failed",
                    "error": self.error,
                })
        else:
            self._finish()

    async def _restart_single_segment(self) -> None:
        log.info(
            "server_does_not_support_ranges",
            download_id=self.spec.download_id,
            url=self.spec.url,
        )
        for part in self.state.part_paths:
            try:
                part.unlink()
            except OSError:
                pass
        self.spec.connections_max = 1
        self.state.segments = [SegmentState(index=0, start=0, end=None, written=0)]
        self.state.state = "downloading"
        await self._run_inner()

    def _finish(self) -> None:
        self._cleanup_sidecar()
        self._set_state("completed")

    # ----- internals -----

    async def _analyze_with_retry(self) -> RemoteFileInfo:
        from ..retry.policy import PermanentError, is_retryable_exception

        last_error: Exception | None = None
        for attempt in range(1, max(1, self.spec.retry_max) + 1):
            try:
                return await analyze(
                    self.client,
                    self.spec.url,
                    headers=self.spec.headers,
                    timeout=self.spec.timeout,
                )
            except Exception as exc:
                if not is_retryable_exception(exc):
                    raise
                last_error = exc
                if attempt >= self.spec.retry_max:
                    break
                await asyncio.sleep(self.spec.retry_base * (2 ** (attempt - 1)))
        raise last_error or PermanentError("resource analysis failed")

    def _reject_html_substitute(self, info: RemoteFileInfo) -> None:
        """Refuse to write an HTML error page over a binary filename."""
        if should_reject_html_body(info.content_type, self.spec.final_path.name):
            raise PermanentError(
                "server returned an HTML page instead of the requested file "
                "(the link may be broken, removed, or behind a login)"
            )

    def _plan_segments(self) -> None:
        total = self.state.total_size
        count = self.spec.connections_max if total and total > 0 else 1
        ranges = plan_segments(total, count)
        self.state.segments = [
            SegmentState(index=index, start=start, end=end)
            for index, (start, end) in enumerate(ranges)
        ]

    def _reconcile_segments_for_size(self) -> None:
        """Adjust a resumed segment plan when the remote size changed.

        The plan is normally only built once; resuming re-probes the resource,
        which can report a different total size (e.g. a file that grew or was
        replaced). Without this, a larger file would be merged truncated, and a
        smaller one would request ranges past EOF. Boundaries are preserved so
        existing part files keep mapping to the same byte ranges; only the last
        segment's end is extended/shrunk and segments beyond the new EOF are
        discarded.
        """
        total = self.state.total_size
        segments = self.state.segments
        if total is None or not segments:
            return
        last = segments[-1]
        if last.end is None or last.end == total - 1:
            return
        if total - 1 > last.end:
            last.end = total - 1
            return
        # File shrank: clamp every segment to the new EOF and drop any that
        # now start past it.
        for segment in list(segments):
            if segment.start > total - 1:
                self.state.segments.remove(segment)
                try:
                    self.state.part_path(segment.index).unlink(missing_ok=True)
                except OSError:
                    pass
            elif segment.end is not None and segment.end > total - 1:
                segment.end = total - 1
        # Trim any stale tail bytes written to the clamped final segment.
        if self.state.segments:
            tail = self.state.segments[-1]
            final_part = self.state.part_path(tail.index)
            if final_part.exists():
                try:
                    new_length = tail.length or 0
                    if final_part.stat().st_size > new_length:
                        with open(final_part, "r+b") as handle:
                            handle.truncate(new_length)
                except OSError:
                    pass

    def _merge_parts(self) -> None:
        final = self.spec.final_path
        final.parent.mkdir(parents=True, exist_ok=True)
        with open(final, "wb") as out:
            for part in self.state.part_paths:
                if part.exists():
                    with open(part, "rb") as fh:
                        shutil.copyfileobj(fh, out)
        for part in self.state.part_paths:
            try:
                part.unlink()
            except OSError:
                pass

    def _cleanup_sidecar(self) -> None:
        try:
            MClipState.sidecar_for(str(self.spec.final_path)).unlink()
        except OSError:
            pass

    def _on_segment_done(self, segment: SegmentState) -> None:
        now = time.monotonic()
        if now - self._last_persist >= self.persist_interval:
            self._last_persist = now
            self._persist()

    def _persist(self) -> None:
        if self.state.state == "completed":
            return
        try:
            self.state.save()
        except OSError as exc:
            log.warning("mclip_save_failed", error=str(exc))

    def _fail(self, message: str) -> None:
        self.state.state = "failed"
        self.result_state = "failed"
        self.error = message
        self._emit(
            Events.DOWNLOAD_STATE_CHANGED,
            {"id": self.spec.download_id, "state": "failed", "error": message},
        )

    def _set_state(self, state: str) -> None:
        self.state.state = state
        self.result_state = state
        self._emit(
            Events.DOWNLOAD_STATE_CHANGED,
            {"id": self.spec.download_id, "state": state},
        )

    def _emit(self, name: str, payload: dict) -> None:
        if self.bus is not None:
            self.bus.post(name, payload)

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.report_interval)
            if self.bus is None:
                continue
            downloaded = self.state.bytes_downloaded
            total = self.state.total_size
            speed = self._monitor.speed()
            fraction = (downloaded / total) if total else None
            self.bus.post(Events.PROGRESS_UPDATED, {
                "id": self.spec.download_id,
                "downloaded": downloaded,
                "total": total,
                "fraction": fraction,
                "speed": speed,
            })
            self.bus.post(Events.SPEED_UPDATED, {
                "id": self.spec.download_id,
                "speed": speed,
            })
            self.bus.post(Events.CONNECTIONS_UPDATED, {
                "id": self.spec.download_id,
                "active": self._active_connections,
                "max": self.spec.connections_max,
            })


class MagnetoCore:
    """Shared engine resources: HTTP client, global rate limiter, active tasks."""

    def __init__(
        self,
        *,
        bus=None,
        client_config: ClientConfig | None = None,
        bandwidth_bytes_per_second: float = 0.0,
        bandwidth_capacity: float | None = None,
    ) -> None:
        self.bus = bus
        self.client_config = client_config or ClientConfig()
        self._client = build_client(self.client_config)
        self._limiter = RateLimiter(
            bandwidth_bytes_per_second, capacity=bandwidth_capacity
        )
        self._allocator = BandwidthAllocator(bandwidth_bytes_per_second)
        self._tasks: dict[int, DownloadTask] = {}

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    @property
    def allocator(self) -> BandwidthAllocator:
        return self._allocator

    def set_bandwidth(self, bytes_per_second: float) -> None:
        self._limiter.set_rate(bytes_per_second)
        self._allocator.set_total(bytes_per_second)
        self._redistribute()

    def _redistribute(self) -> None:
        """Share the bandwidth budget among active tasks by priority."""
        if self._allocator.total <= 0:
            for task in self._tasks.values():
                task.set_bandwidth(0.0)
            return
        weights = {
            task.spec.download_id: self._allocator.weight_for(task.spec.priority)
            for task in self._tasks.values()
        }
        rates = self._allocator.allocate(weights)
        for download_id, rate in rates.items():
            task = self._tasks.get(download_id)
            if task is not None:
                task.set_bandwidth(rate)

    def submit(self, spec: DownloadSpec, state: MClipState | None = None) -> DownloadTask:
        client, close_client = self._resolve_client(spec)
        limiter = self._limiter
        if self._allocator.total > 0:
            limiter = RateLimiter(
                self._limiter.rate / max(1, len(self._tasks) + 1),
                capacity=self._limiter.capacity,
            )
        task = DownloadTask(
            spec=spec,
            state=state,
            client=client,
            limiter=limiter,
            bus=self.bus,
            close_client=close_client,
        )
        self._tasks[spec.download_id] = task
        self._redistribute()
        return task

    def _resolve_client(
        self, spec: DownloadSpec
    ) -> tuple[httpx.AsyncClient, bool]:
        """Use the shared client unless the spec needs special transport."""
        needs_special = bool(
            spec.proxy
            or spec.auth
            or spec.cookies
            or spec.user_agent != self.client_config.user_agent
        )
        if not needs_special:
            return self._client, False
        config = replace(
            self.client_config,
            proxy=spec.proxy,
            auth=spec.auth,
            cookies=spec.cookies,
            user_agent=spec.user_agent,
        )
        return build_client(config), True

    def get(self, download_id: int) -> DownloadTask | None:
        return self._tasks.get(download_id)

    def set_priority(self, download_id: int, priority: int) -> None:
        task = self._tasks.get(download_id)
        if task is not None:
            task.spec.priority = int(priority)
            self._redistribute()

    def pause(self, download_id: int) -> None:
        task = self.get(download_id)
        if task is not None:
            task.pause()

    def resume(self, download_id: int) -> None:
        task = self.get(download_id)
        if task is not None:
            task.resume()

    def cancel(self, download_id: int) -> None:
        task = self.get(download_id)
        if task is not None:
            task.cancel()

    def active_tasks(self) -> list[DownloadTask]:
        return [task for task in self._tasks.values() if not task.is_terminal()]

    def remove_task(self, download_id: int) -> None:
        self._tasks.pop(download_id, None)
        self._redistribute()

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await self._client.aclose()
        self._tasks.clear()


def spec_from_url(
    *,
    download_id: int,
    url: str,
    save_dir: Path,
    filename: str | None = None,
    headers: dict[str, str] | None = None,
    auth: AuthSpec | None = None,
    proxy: ProxySpec | None = None,
    connections_max: int = 8,
    retry_max: int = 5,
    retry_base: float = 1.0,
    timeout: float = 30.0,
    hash_algo: str | None = None,
    hash_expected: str | None = None,
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    cookies: dict[str, str] | None = None,
    priority: int = 0,
) -> DownloadSpec:
    """Convenience builder that sanitizes the destination filename."""
    safe_name = sanitize_filename(filename) if filename else None
    return DownloadSpec(
        download_id=download_id,
        url=url,
        save_dir=save_dir,
        filename=safe_name or sanitize_filename(url.rsplit("/", 1)[-1] or "download"),
        headers=headers or {},
        auth=auth,
        proxy=proxy,
        cookies=cookies or {},
        connections_max=connections_max,
        retry_max=retry_max,
        retry_base=retry_base,
        timeout=timeout,
        hash_algo=hash_algo,
        hash_expected=hash_expected,
        user_agent=user_agent,
        priority=priority,
    )
