"""Single-range segment downloader with retry, pause, and cancel support."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx

from magnetoclip.network.http.range import range_header
from magnetoclip.services.logging.setup import get_logger

from ..resume.mclip import SegmentState
from ..retry.policy import (
    PermanentError,
    RangeNotSupported,
    RetryableError,
    SegmentAborted,
    backoff_delay,
    classify_http_status,
    is_retryable_http_status,
)
from .throttle import RateLimiter
from .types import DownloadSpec

log = get_logger(__name__)


class SegmentDownloader:
    """Downloads one byte range, retrying transient failures.

    ``stop_event`` lets the pool ask the downloader to end cleanly at the next
    chunk boundary (used by tail-rebalancing). ``segment.end`` may be shrunk
    under the running downloader; each chunk is clamped to the current range so
    ``written`` can never overshoot the (possibly narrowed) end.

    With ``random_access=True`` the segment writes directly into a shared final
    file at ``base_offset + written`` instead of its own part file. Each chunk
    is flushed before ``written`` advances so a crash can never leave the
    persisted progress ahead of the bytes actually handed to the OS.
    """

    def __init__(
        self,
        segment: SegmentState,
        spec: DownloadSpec,
        *,
        target_path: Path,
        base_offset: int = 0,
        random_access: bool = False,
        limiter: RateLimiter | None = None,
        running: asyncio.Event | None = None,
        cancel: asyncio.Event | None = None,
        stop_event: asyncio.Event | None = None,
        report: Callable[[int], None] | None = None,
    ) -> None:
        self.segment = segment
        self.spec = spec
        self.target_path = target_path
        self.base_offset = base_offset
        self.random_access = random_access
        self.limiter = limiter
        self.running = running
        self.cancel = cancel
        self.stop_event = stop_event
        self.report = report

    async def run(self, client: httpx.AsyncClient) -> None:
        for attempt in range(1, max(1, self.spec.retry_max) + 1):
            await self._wait_while_paused()
            try:
                await self._attempt(client)
                self.segment.status = "completed"
                return
            except SegmentAborted:
                raise
            except RangeNotSupported:
                raise
            except (RetryableError, httpx.TimeoutException, httpx.TransportError) as exc:
                self.segment.attempts = attempt
                if attempt >= self.spec.retry_max:
                    raise PermanentError(
                        f"segment {self.segment.index} failed after {attempt} attempts"
                    ) from exc
                await self._wait_while_paused()
                await asyncio.sleep(
                    backoff_delay(attempt, base=self.spec.retry_base)
                )
            except httpx.HTTPStatusError as exc:
                if is_retryable_http_status(exc.response.status_code):
                    self.segment.attempts = attempt
                    if attempt >= self.spec.retry_max:
                        raise PermanentError(
                            f"segment {self.segment.index} HTTP "
                            f"{exc.response.status_code}"
                        ) from exc
                    await self._wait_while_paused()
                    await asyncio.sleep(
                        backoff_delay(attempt, base=self.spec.retry_base)
                    )
                    continue
                raise classify_http_status(exc.response.status_code)(
                    f"HTTP {exc.response.status_code}"
                ) from exc

    async def _attempt(self, client: httpx.AsyncClient) -> None:
        start = self.segment.start + self.segment.written
        if self.segment.end is not None and start > self.segment.end:
            return  # already downloaded
        if self.cancel is not None and self.cancel.is_set():
            raise SegmentAborted()

        headers = {**self.spec.headers, **range_header(start, self.segment.end)}
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)

        async with client.stream(
            "GET", self.spec.url, headers=headers, timeout=timeout
        ) as response:
            if response.status_code == 416:
                self.segment.written = (
                    self.segment.length if self.segment.length is not None else 0
                )
                return
            if response.status_code == 200:
                single_open = start == 0 and self.segment.end is None
                if not single_open:
                    raise RangeNotSupported()
            if response.status_code != 200:
                if response.status_code in (206, 200):
                    pass
                else:
                    response.raise_for_status()

            mode = "r+b" if self.random_access else (
                "ab" if self.segment.written > 0 else "wb"
            )
            cursor = start
            with open(self.target_path, mode) as fh:
                if self.random_access:
                    fh.seek(self.base_offset + self.segment.written)
                async for chunk in response.aiter_bytes():
                    await self._wait_while_paused()
                    if self.cancel is not None and self.cancel.is_set():
                        raise SegmentAborted()
                    if self.segment.end is not None:
                        remaining = self.segment.end - cursor + 1
                        if remaining <= 0:
                            break
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                    if not chunk:
                        continue
                    if self.limiter is not None:
                        await self.limiter.acquire(len(chunk))
                    fh.write(chunk)
                    if self.random_access:
                        fh.flush()
                    cursor += len(chunk)
                    self.segment.written += len(chunk)
                    if self.report is not None:
                        self.report(len(chunk))
                    if self.stop_event is not None and self.stop_event.is_set():
                        break

    async def _wait_while_paused(self) -> None:
        while self.running is not None and not self.running.is_set():
            if self.cancel is not None and self.cancel.is_set():
                raise SegmentAborted()
            await asyncio.sleep(0.05)
