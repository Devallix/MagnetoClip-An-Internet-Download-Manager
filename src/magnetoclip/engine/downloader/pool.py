"""Concurrent segment pool with adaptive connection ramp-up."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from ..resume.mclip import SegmentState
from ..retry.policy import PermanentError, RangeNotSupported, SegmentAborted
from .allocator import AdaptiveAllocator
from .segment import SegmentDownloader
from .throttle import RateLimiter, ThroughputMonitor
from .types import DownloadSpec


class SegmentPool:
    """Runs planned segments with a dynamically growing worker count."""

    def __init__(
        self,
        segments: list[SegmentState],
        spec: DownloadSpec,
        *,
        client: httpx.AsyncClient,
        limiter: RateLimiter | None = None,
        running: asyncio.Event | None = None,
        cancel: asyncio.Event | None = None,
        monitor: ThroughputMonitor | None = None,
        on_segment_done=None,
    ) -> None:
        self._queue: asyncio.Queue[SegmentState] = asyncio.Queue()
        for segment in segments:
            self._queue.put_nowait(segment)
        self.spec = spec
        self.client = client
        self.limiter = limiter
        self.running = running
        self.cancel = cancel
        self.monitor = monitor or ThroughputMonitor()
        self.on_segment_done = on_segment_done
        self.allocator = AdaptiveAllocator(spec.connections_max)

        self._workers: list[asyncio.Task] = []
        self._fatal: Exception | None = None
        self._fatal_event = asyncio.Event()
        self._abort_event = asyncio.Event()

    @property
    def current_connections(self) -> int:
        return sum(1 for worker in self._workers if not worker.done())

    async def run(self) -> None:
        for _ in range(self.allocator.initial):
            self._workers.append(asyncio.create_task(self._worker()))
        ramp = asyncio.create_task(self._ramp())
        join_task = asyncio.create_task(self._queue.join())
        stop_task = asyncio.create_task(self._wait_stop())

        try:
            await asyncio.wait(
                {join_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            ramp.cancel()
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(ramp, *self._workers, return_exceptions=True)
            for task in (join_task, stop_task):
                if not task.done():
                    task.cancel()

        if self._fatal is not None:
            raise self._fatal
        if self._abort_event.is_set():
            raise SegmentAborted()

    async def _wait_stop(self) -> None:
        fatal_task = asyncio.create_task(self._fatal_event.wait())
        abort_task = asyncio.create_task(self._abort_event.wait())
        try:
            await asyncio.wait(
                {fatal_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (fatal_task, abort_task):
                task.cancel()

    async def _worker(self) -> None:
        try:
            while True:
                segment = await self._queue.get()
                try:
                    await self._download(segment)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except SegmentAborted:
            self._abort_event.set()
            raise
        except (PermanentError, RangeNotSupported) as exc:
            if self._fatal is None:
                self._fatal = exc
                self._fatal_event.set()
            raise

    async def _download(self, segment: SegmentState) -> None:
        segment.status = "active"
        downloader = SegmentDownloader(
            segment,
            self.spec,
            part_path=Path(f"{self.spec.final_path}.part{segment.index}"),
            limiter=self.limiter,
            running=self.running,
            cancel=self.cancel,
            report=self.monitor.add,
        )
        await downloader.run(self.client)
        segment.status = "completed"
        if self.on_segment_done is not None:
            self.on_segment_done(segment)

    async def _ramp(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if now - last >= self.allocator.ramp_interval:
                last = now
                target = self.allocator.evaluate(self.monitor.speed())
                while len(self._workers) < target:
                    self._workers.append(asyncio.create_task(self._worker()))
