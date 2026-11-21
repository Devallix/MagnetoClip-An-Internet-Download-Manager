"""Bandwidth limiting and throughput measurement."""

from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    """Token-bucket rate limiter shared across downloads/segments.

    ``rate`` is bytes per second; ``0`` means unlimited. Safe to call from
    cooperative async tasks running on the same event loop.
    """

    def __init__(self, rate: float = 0.0, capacity: float | None = None) -> None:
        self._rate = max(0.0, rate)
        self._capacity = (
            capacity
            if capacity is not None
            else max(self._rate * 2, 1024 * 1024)
        )
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._updated = asyncio.Event()
        self._updated.set()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def set_rate(self, rate: float) -> None:
        self._rate = max(0.0, rate)
        if self._capacity < self._rate * 2:
            self._capacity = max(self._rate * 2, 1024 * 1024)
            self._tokens = min(self._tokens, self._capacity)
        self._updated.set()

    async def acquire(self, amount: int) -> None:
        remaining = amount
        while remaining > 0:
            if self._rate <= 0:
                return
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            take = min(self._tokens, remaining)
            if take > 0:
                self._tokens -= take
                remaining -= take
                continue
            wait = 1.0 / self._rate  # time to accumulate one byte
            self._updated.clear()
            try:
                await asyncio.wait_for(self._updated.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass


class ThroughputMonitor:
    """Sliding-window throughput measurement with EMA smoothing."""

    def __init__(self, window: float = 3.0) -> None:
        self.window = window
        self._points: deque[tuple[float, int]] = deque()
        self._total = 0
        self._last_speed = 0.0

    def add(self, amount: int) -> None:
        self._total += amount
        now = time.monotonic()
        self._points.append((now, amount))
        cutoff = now - self.window
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()

    @property
    def total(self) -> int:
        return self._total

    def speed(self) -> float:
        if not self._points:
            return self._last_speed
        in_window = sum(amount for _, amount in self._points)
        instant = in_window / self.window
        if self._last_speed == 0:
            self._last_speed = instant
        else:
            self._last_speed = 0.7 * instant + 0.3 * self._last_speed
        return self._last_speed
