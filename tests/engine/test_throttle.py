import asyncio

import pytest

from magnetoclip.engine.downloader.allocator import AdaptiveAllocator
from magnetoclip.engine.downloader.throttle import RateLimiter, ThroughputMonitor


@pytest.mark.asyncio
async def test_rate_limiter_unlimited():
    limiter = RateLimiter(0)
    await limiter.acquire(10_000)
    assert limiter.rate == 0


@pytest.mark.asyncio
async def test_rate_limiter_throttles():
    limiter = RateLimiter(1000, capacity=1000)  # 1000 bytes/s, no burst
    start = asyncio.get_event_loop().time()
    await limiter.acquire(2000)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 1.0


@pytest.mark.asyncio
async def test_rate_limiter_rate_change():
    limiter = RateLimiter(1000)
    limiter.set_rate(0)  # unlimited after change
    await limiter.acquire(100_000)  # should not block


def test_throughput_monitor():
    monitor = ThroughputMonitor(window=1.0)
    monitor.add(1000)
    monitor.add(1000)
    assert monitor.total == 2000
    speed = monitor.speed()
    assert speed >= 0


def test_allocator_starts_at_initial():
    allocator = AdaptiveAllocator(16)
    assert allocator.active == 2
    assert allocator.evaluate(0) == 2  # first sample sets baseline


def test_allocator_grows_on_improvement():
    allocator = AdaptiveAllocator(16, minimum_gain=0.1)
    allocator.evaluate(1000)
    assert allocator.evaluate(2000) == 4
    assert allocator.evaluate(4000) == 8
    assert allocator.evaluate(8000) == 16


def test_allocator_stops_when_saturated():
    allocator = AdaptiveAllocator(16, minimum_gain=0.1)
    allocator.evaluate(1000)
    assert allocator.evaluate(1100) == 4  # 10% gain
    assert allocator.evaluate(1110) == 4  # <10% gain -> stopped
    assert allocator.evaluate(5000) == 4  # no further growth


def test_allocator_never_exceeds_max():
    allocator = AdaptiveAllocator(3)
    allocator.evaluate(100)
    allocator.evaluate(200)
    allocator.evaluate(400)
    assert allocator.active <= 3
