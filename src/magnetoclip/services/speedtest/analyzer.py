"""SpeedAnalyzer: historical stats, throttle detection, and CSV export."""

from __future__ import annotations

import csv
import io
from typing import Any

from ...database.repositories import SpeedTestRepository


class SpeedAnalyzer:
    """Analyse stored speed test results."""

    def __init__(self, context) -> None:
        self.context = context
        self.session_factory = context.session_factory

    def _repo(self, session):
        return SpeedTestRepository(session)

    def stats(self) -> dict[str, Any]:
        with self.session_factory() as session:
            return self._repo(session).stats()

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = self._repo(session).largest(limit)
            return [
                {
                    "ts": row.ts.isoformat() if row.ts else "",
                    "download_mbps": row.download_mbps,
                    "upload_mbps": row.upload_mbps,
                    "latency_ms": row.latency_ms,
                    "server_name": row.server_name,
                    "error": row.error,
                }
                for row in rows
            ]

    def throttle_analysis(self) -> dict[str, Any]:
        """Detect possible ISP throttling from recent history.

        Compares the most recent test against the trailing average; a large
        drop (beyond the configured sensitivity) is flagged as a likely
        throttling event.
        """
        with self.session_factory() as session:
            rows = self._repo(session).largest(20)
        speeds = [r.download_mbps for r in rows if r.download_mbps]
        if len(speeds) < 2:
            return {"flagged": False, "reason": "Not enough data"}
        latest = speeds[0]
        baseline = sum(speeds[1:]) / len(speeds[1:])
        sensitivity = float(
            self.context.settings.get("speedtest.throttle_sensitivity", 0.3)
        )
        if baseline <= 0:
            return {"flagged": False, "reason": "Baseline unavailable"}
        drop_ratio = 1.0 - (latest / baseline)
        flagged = drop_ratio >= sensitivity
        return {
            "flagged": flagged,
            "latest_mbps": round(latest, 2),
            "baseline_mbps": round(baseline, 2),
            "drop_pct": round(drop_ratio * 100, 1),
            "reason": (
                f"Speed {latest:.1f} Mbps is {drop_ratio*100:.0f}% below "
                f"baseline {baseline:.1f} Mbps"
            ),
        }

    def csv_export(self, limit: int = 500) -> str:
        """Return a CSV string of recent results for saving."""
        with self.session_factory() as session:
            rows = self._repo(session).largest(limit)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["timestamp", "download_mbps", "upload_mbps", "latency_ms", "server_name", "error"]
        )
        for row in rows:
            writer.writerow(
                [
                    row.ts.isoformat() if row.ts else "",
                    row.download_mbps,
                    row.upload_mbps,
                    row.latency_ms,
                    row.server_name,
                    row.error,
                ]
            )
        return buffer.getvalue()

    def prune(self, keep: int = 500) -> int:
        with self.session_factory() as session:
            return self._repo(session).prune(keep=keep)

    def add_result(self, result: dict[str, Any]) -> None:
        with self.session_factory() as session:
            self._repo(session).add(
                download_mbps=result.get("download_mbps"),
                upload_mbps=result.get("upload_mbps"),
                latency_ms=result.get("latency_ms"),
                server_name=result.get("server_name"),
                server_url=result.get("server_url"),
                isp_name=result.get("isp_name"),
                test_size_mb=result.get("test_size_mb") or 25,
                error=result.get("error"),
            )
