"""Network speed test: measure download/upload throughput and latency."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from magnetoclip.core.events.bus import Events

ProgressCallback = Callable[[float], None]

# Default measurement targets. ``download`` should resolve to a large file;
# ``upload`` should accept POSTed bytes and return quickly.
DEFAULT_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
DEFAULT_UPLOAD_URL = "https://speed.cloudflare.com/__up"


class SpeedTester:
    """Run a download/upload/latency probe and report throughput.

    The tester streams a chunked HTTP response, sampling elapsed time / bytes
    to emit progress events and to compute a stable average throughput.
    """

    def __init__(self, context) -> None:
        self.context = context
        self._client: httpx.AsyncClient | None = None

    def _client_sync(self) -> httpx.Client:
        timeout = float(
            self.context.settings.get("network.timeout_seconds", 30)
        ) + 60
        return httpx.Client(timeout=timeout, follow_redirects=True)

    async def run(
        self,
        url: str = "",
        *,
        test_size_mb: int = 25,
        upload: bool = False,
    ) -> dict:
        """Run a speed probe; returns a result dict (see ``measure``)."""
        events = self.context.events
        result = await self.measure(url=url, test_size_mb=test_size_mb, upload=upload)
        events.post(
            Events.SPEED_TEST_PROGRESS,
            {
                "download_mbps": result.get("download_mbps"),
                "upload_mbps": result.get("upload_mbps"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error"),
            },
        )
        return result

    async def measure(
        self,
        *,
        url: str = "",
        test_size_mb: int = 25,
        upload: bool = False,
    ) -> dict:
        """Measure throughput and latency. Runs synchronously (async)."""
        try:
            if not upload:
                return await self._measure_download(url, test_size_mb)
            return await self._measure_upload(url, test_size_mb)
        except Exception as exc:  # noqa: BLE001
            return {
                "download_mbps": None,
                "upload_mbps": None,
                "latency_ms": None,
                "server_name": None,
                "server_url": url,
                "isp_name": None,
                "test_size_mb": test_size_mb,
                "error": str(exc),
            }

    async def measure_full(
        self,
        *,
        url: str = "",
        test_size_mb: int = 25,
    ) -> dict:
        """Run a combined download + upload + latency measurement.

        The upload phase runs immediately after the download phase so a single
        "Run Test" yields both directions and populates every result card.
        """
        try:
            dl = await self._measure_download(url, test_size_mb)
            ul = await self._measure_upload(url, test_size_mb)
            error = None
            if dl.get("download_mbps") is None and dl.get("error"):
                error = dl.get("error")
            elif ul.get("upload_mbps") is None and ul.get("error"):
                error = ul.get("error")
            return {
                "download_mbps": dl.get("download_mbps"),
                "upload_mbps": ul.get("upload_mbps"),
                "latency_ms": dl.get("latency_ms") or ul.get("latency_ms"),
                "server_name": dl.get("server_name") or ul.get("server_name"),
                "server_url": dl.get("server_url") or ul.get("server_url"),
                "isp_name": None,
                "test_size_mb": test_size_mb,
                "error": error,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "download_mbps": None,
                "upload_mbps": None,
                "latency_ms": None,
                "server_name": None,
                "server_url": url,
                "isp_name": None,
                "test_size_mb": test_size_mb,
                "error": str(exc),
            }

    async def _measure_download(self, url: str, test_size_mb: int) -> dict:
        events = self.context.events
        target_url = url or str(
            self.context.settings.get(
                "speedtest.test_server", DEFAULT_DOWNLOAD_URL
            )
        ) or DEFAULT_DOWNLOAD_URL

        # Latency probe against the host.
        latency = await self._probe_latency(target_url)

        start = time.monotonic()
        total = 0
        with self._client_sync() as client, client.stream("GET", target_url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(64 * 1024):
                    total += len(chunk)
                    elapsed = time.monotonic() - start
                    if elapsed > 0:
                        mbps = (total * 8) / elapsed / 1_000_000
                        events.post(
                            Events.SPEED_TEST_PROGRESS,
                            {
                                "progress": min(1.0, total / (test_size_mb * 1_000_000)),
                                "download_mbps": round(mbps, 2),
                                "phase": "download",
                            },
                        )
        elapsed = time.monotonic() - start
        mbps = (total * 8) / elapsed / 1_000_000 if elapsed > 0 else 0.0
        return {
            "download_mbps": round(mbps, 2),
            "upload_mbps": None,
            "latency_ms": latency,
            "server_name": self._server_name(target_url),
            "server_url": target_url,
            "isp_name": None,
            "test_size_mb": test_size_mb,
            "error": None,
        }

    async def _measure_upload(self, url: str, test_size_mb: int) -> dict:
        events = self.context.events
        target_url = url or str(
            self.context.settings.get(
                "speedtest.test_server", DEFAULT_UPLOAD_URL
            )
        ) or DEFAULT_UPLOAD_URL

        # Latency probe against the host (gives the latency card a value even
        # for upload-only tests).
        latency = await self._probe_latency(target_url)

        payload = b"x" * (1024 * 1024)
        start = time.monotonic()
        total = 0
        with self._client_sync() as client:
            for _ in range(max(1, test_size_mb)):
                client.post(target_url, content=payload)
                total += len(payload)
                elapsed = time.monotonic() - start
                mbps = (total * 8) / elapsed / 1_000_000 if elapsed > 0 else 0.0
                events.post(
                    Events.SPEED_TEST_PROGRESS,
                    {
                        "progress": total / (test_size_mb * 1_000_000),
                        "upload_mbps": round(mbps, 2),
                        "phase": "upload",
                    },
                )
        elapsed = time.monotonic() - start
        mbps = (total * 8) / elapsed / 1_000_000 if elapsed > 0 else 0.0
        return {
            "download_mbps": None,
            "upload_mbps": round(mbps, 2),
            "latency_ms": latency,
            "server_name": self._server_name(target_url),
            "server_url": target_url,
            "isp_name": None,
            "test_size_mb": test_size_mb,
            "error": None,
        }

    async def _probe_latency(self, url: str) -> float | None:
        try:
            with self._client_sync() as client:
                best = None
                for _ in range(3):
                    start = time.monotonic()
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                    elapsed = (time.monotonic() - start) * 1000
                    best = elapsed if best is None else min(best, elapsed)
                return round(best, 1) if best is not None else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _server_name(url: str) -> str | None:
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname
            return host or None
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self._client = None
