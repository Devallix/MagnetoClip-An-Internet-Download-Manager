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
    """Runs planned segments with a dynamically growing worker count.

    Workers also rebalance the tail: once the queue drains while a segment
    still has a large remainder, the slowest active segment is split and its
    uncut remainder is handed to a free worker instead of letting one slow
    segment stall the whole download. Splits are byte-exact (``segment.end``
    is narrowed and the downloader clamps to it at the next chunk), so parts
    stay non-overlapping and merges remain correct.
    """

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
        in_place: bool = False,
        assist_interval: float = 1.0,
        assist_min_remaining: int = 2 * 1024 * 1024,
        split_grace: float = 10.0,
    ) -> None:
        self._queue: asyncio.Queue[SegmentState] = asyncio.Queue()
        for segment in segments:
            self._queue.put_nowait(segment)
        # The engine passes its live state.segments list; a rebalance appends
        # the split suffix here so persistence/merge see it too.
        self._segments = segments
        self.spec = spec
        self.client = client
        self.limiter = limiter
        self.running = running
        self.cancel = cancel
        self.monitor = monitor or ThroughputMonitor()
        self.on_segment_done = on_segment_done
        self.in_place = in_place
        self.allocator = AdaptiveAllocator(spec.connections_max)
        self.assist_interval = assist_interval
        self.assist_min_remaining = assist_min_remaining

        self._workers: list[asyncio.Task] = []
        self._fatal: Exception | None = None
        self._fatal_event = asyncio.Event()
        self._abort_event = asyncio.Event()

        self._actives: dict[int, SegmentState] = {}
        self._stop_events: dict[int, asyncio.Event] = {}
        self._segment_workers: dict[int, asyncio.Task] = {}
        self._progress: dict[int, tuple[float, int]] = {}
        self._rates: dict[int, float] = {}
        self._splitting: set[int] = set()
        self._split_at: dict[int, float] = {}
        self._last_assist = 0.0

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
        stop_event = asyncio.Event()
        self._actives[segment.index] = segment
        self._stop_events[segment.index] = stop_event
        self._segment_workers[segment.index] = asyncio.current_task()
        try:
            if self.in_place:
                target_path = self.spec.final_path
                base_offset = segment.start
            else:
                target_path = Path(f"{self.spec.final_path}.part{segment.index}")
                base_offset = 0
            downloader = SegmentDownloader(
                segment,
                self.spec,
                target_path=target_path,
                base_offset=base_offset,
                random_access=self.in_place,
                limiter=self.limiter,
                running=self.running,
                cancel=self.cancel,
                stop_event=stop_event,
                report=self.monitor.add,
            )
            await downloader.run(self.client)
            segment.status = "completed"
            if self.on_segment_done is not None:
                self.on_segment_done(segment)
        finally:
            self._actives.pop(segment.index, None)
            self._stop_events.pop(segment.index, None)
            self._segment_workers.pop(segment.index, None)
            self._progress.pop(segment.index, None)
            self._rates.pop(segment.index, None)
            self._splitting.discard(segment.index)

    async def _ramp(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if now - last >= self.allocator.ramp_interval:
                last = now
                target = self.allocator.evaluate(self.monitor.speed())
                active_workers = sum(1 for w in self._workers if not w.done())
                while active_workers < target:
                    self._workers.append(asyncio.create_task(self._worker()))
                    active_workers += 1
            if now - self._last_assist >= self.assist_interval:
                self._last_assist = now
                self._refresh_speeds(now)
                self._kill_stalled(now)
                self._assist()

    def _refresh_speeds(self, now: float) -> None:
        for index, segment in list(self._actives.items()):
            prev_time, prev_written = self._progress.get(
                index, (now, segment.written)
            )
            if prev_time < now:
                self._rates[index] = max(
                    0.0, (segment.written - prev_written) / (now - prev_time)
                )
            self._progress[index] = (now, segment.written)

    def _kill_stalled(self, now: float) -> None:
        for index, started_at in list(self._split_at.items()):
            if index not in self._actives:
                self._split_at.pop(index, None)
                continue
            if now - started_at < self.split_grace:
                continue
            worker = self._segment_workers.get(index)
            if worker is not None and not worker.done():
                worker.cancel()
                self._splitting.discard(index)
            self._split_at.pop(index, None)

    def _segment_speed(self, index: int) -> float:
        return self._rates.get(index, 0.0)

    @staticmethod
    def _remaining(segment: SegmentState) -> int:
        if segment.end is None:
            return 0
        return max(0, segment.end - (segment.start + segment.written) + 1)

    def _assist(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            return
        if self.running is not None and not self.running.is_set():
            return
        if not self._queue.empty() or not self._actives:
            return
        active_workers = sum(1 for worker in self._workers if not worker.done())
        if active_workers < 2 or len(self._actives) < 1:
            return
        speeds = {index: self._segment_speed(index) for index in self._actives}
        best = max(speeds.values(), default=0.0)
        if len(self._actives) == 1:
            # Lone trailing segment: spread it across the idle workers.
            index = next(iter(self._actives))
            segment = self._actives[index]
            if (
                index in self._splitting
                or segment.end is None
                or self._remaining(segment) <= self.assist_min_remaining
            ):
                return
            self._split(segment)
            return
        candidates = [
            index
            for index, segment in self._actives.items()
            if index not in self._splitting
            and segment.end is not None
            and self._remaining(segment) > self.assist_min_remaining
            and (best <= 0 or speeds[index] < best * 0.5)
        ]
        if not candidates:
            return
        target_index = max(
            candidates, key=lambda index: self._remaining(self._actives[index])
        )
        self._split(self._actives[target_index])

    def _split(self, segment: SegmentState) -> None:
        if segment.end is None or segment.index in self._splitting:
            return
        speed = self._segment_speed(segment.index)
        remaining = self._remaining(segment)
        base = segment.start + segment.written
        if speed <= 0:
            # Stalled: hand the whole uncut remainder to a fresh worker.
            mid = base
        else:
            mid = base + remaining // 2

        original_end = segment.end
        new_index = max(
            (seg.index for seg in self._segments), default=segment.index
        )

        suffix = SegmentState(
            index=new_index + 1,
            start=max(mid, base),
            end=original_end,
            written=0,
            status="pending",
        )
        if suffix.start > original_end:
            return  # nothing left to help with
        segment.end = suffix.start - 1
        self._splitting.add(segment.index)
        self._split_at[segment.index] = time.monotonic()
        self._segments.append(suffix)
        self._queue.put_nowait(suffix)
        stop_event = self._stop_events.get(segment.index)
        if stop_event is not None:
            stop_event.set()