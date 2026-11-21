"""Network speed monitor: capture, analyse, and export speed tests."""

from __future__ import annotations

import asyncio
from typing import Any

from ...core.events.bus import Events
from .analyzer import SpeedAnalyzer
from .tester import SpeedTester


class SpeedTestService:
    """Facade bundling the tester (write side) and analyzer (read side)."""

    def __init__(self, context) -> None:
        self.context = context
        self.events = context.events
        self.tester = SpeedTester(context)
        self.analyzer = SpeedAnalyzer(context)
        self._auto_task: asyncio.Task | None = None
        self._last_auto_ts = 0.0
        self._start_auto()

    def _auto_enabled(self) -> bool:
        return bool(
            self.context.settings.get("speedtest.auto_test_enabled", False)
        ) and bool(self.context.settings.get("speedtest.enabled", True))

    def _start_auto(self) -> None:
        if not self._auto_enabled():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop yet; the GUI startup path calls start_auto()
            # once the qasync loop is live.
            self._auto_task = None
            return
        self._auto_task = asyncio.create_task(self._auto_loop())

    def start_auto(self) -> None:
        if self._auto_task is not None and not self._auto_task.done():
            return
        self._start_auto()

    async def _auto_loop(self) -> None:
        interval_h = int(
            self.context.settings.get("speedtest.auto_test_interval_hours", 12)
        ) or 12
        while True:
            await asyncio.sleep(interval_h * 3600)
            await self.run_test()

    async def run_test(self, *, upload: bool = False) -> dict[str, Any]:
        """Run a test, persist it, and emit completion event.

        ``upload=True`` runs an upload-only test; otherwise the full test runs
        download, upload, and latency so every result card is populated.
        """
        test_size_mb = int(
            self.context.settings.get("speedtest.test_size_mb", 25)
        )
        if upload:
            result = await self.tester.measure(
                test_size_mb=test_size_mb, upload=True
            )
        else:
            result = await self.tester.measure_full(test_size_mb=test_size_mb)
        self.analyzer.add_result(result)
        self.events.post(
            Events.SPEED_TEST_COMPLETED,
            {
                "download_mbps": result.get("download_mbps"),
                "upload_mbps": result.get("upload_mbps"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error"),
            },
        )
        self._trigger_auto()
        return result

    def _trigger_auto(self) -> None:
        if self._auto_task is None or self._auto_task.done():
            self.start_auto()

    def run_test_blocking(self, *, upload: bool = False) -> dict[str, Any]:
        """Convenience for calling from a worker thread."""
        import asyncio as _asyncio

        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            future = _asyncio.run_coroutine_threadsafe(self.run_test(upload=upload), loop)
            return future.result()
        return _asyncio.run(self.run_test(upload=upload))

    def stats(self) -> dict[str, Any]:
        return self.analyzer.stats()

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.analyzer.history(limit)

    def throttle_analysis(self) -> dict[str, Any]:
        return self.analyzer.throttle_analysis()

    def csv_export(self, limit: int = 500) -> str:
        return self.analyzer.csv_export(limit)

    def close(self) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None
        self.tester.close()


__all__ = ["SpeedAnalyzer", "SpeedTestService", "SpeedTester"]
