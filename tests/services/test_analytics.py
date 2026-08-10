"""Tests for analytics collection and dashboard aggregation."""

from __future__ import annotations

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.services.analytics.collector import StatsCollector
from magnetoclip.services.analytics.dashboard import DashboardService


@pytest.fixture
def context(tmp_path):
    ctx = build_context(
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", log_dir=tmp_path / "log"
    )
    yield ctx
    import asyncio

    asyncio.run(ctx.shutdown())


class TestStatsCollector:
    def test_record_creates_statistic_rows(self, context) -> None:
        download = context.manager.add("https://example.com/a.zip")
        collector = StatsCollector(context, interval=1.0)
        collector.record(download.id, speed=100.0, connections=4)
        collector.flush()
        with context.session_factory() as session:
            from magnetoclip.database.models import DownloadStatistic

            rows = session.query(DownloadStatistic).filter_by(download_id=download.id).all()
            assert len(rows) == 1
            assert rows[0].speed == 100.0
            assert rows[0].connections == 4
            assert rows[0].bandwidth_used == int(100.0 * 1.0)
        collector.close()

    def test_speed_event_throttles_writes(self, context) -> None:
        download = context.manager.add("https://example.com/b.zip")
        collector = StatsCollector(context, interval=60.0)
        for speed in (10.0, 20.0, 30.0):
            collector._on_speed({"id": download.id, "speed": speed})
        collector.flush()
        with context.session_factory() as session:
            from magnetoclip.database.models import DownloadStatistic

            count = session.query(DownloadStatistic).filter_by(download_id=download.id).count()
            assert count <= 2  # far fewer than the 3 events emitted
        collector.close()


class TestDashboard:
    def test_overview_totals(self, context) -> None:
        manager = context.manager
        d1 = manager.add("https://example.com/x.zip")
        d2 = manager.add("https://example.com/y.zip")
        with context.session_factory() as session:
            from magnetoclip.database.models import DownloadStatus

            for download in (d1, d2):
                session.get(type(download), download.id).status = DownloadStatus.completed
                session.get(type(download), download.id).size_downloaded = 1024
            session.commit()

        overview = DashboardService(context).overview()
        assert overview["total"] == 2
        assert overview["completed"] == 2
        assert overview["bytes_downloaded"] == 2048

    def test_daily_activity_returns_padded_days(self, context) -> None:
        dashboard = DashboardService(context)
        days = dashboard.daily_activity(days=7)
        assert len(days) == 7
        assert all(d["count"] == 0 for d in days)

    def test_bandwidth_history_aggregates(self, context) -> None:
        manager = context.manager
        download = manager.add("https://example.com/z.zip")
        collector = StatsCollector(context, interval=1.0)
        collector.record(download.id, speed=500.0)
        collector.flush()
        collector.close()
        history = DashboardService(context).bandwidth_history(days=7)
        today = history[-1]
        assert today["bytes"] == 500

    def test_category_breakdown(self, context) -> None:
        context.categories.add("SciFi", folder="SciFi")
        manager = context.manager
        manager.add("https://example.com/planet.zip", category_name="SciFi")
        breakdown = DashboardService(context).category_breakdown()
        assert any(row["name"] == "SciFi" and row["count"] == 1 for row in breakdown)

    def test_status_counts(self, context) -> None:
        manager = context.manager
        manager.add("https://example.com/one.zip")
        dashboard = DashboardService(context)
        counts = dashboard.status_counts()
        assert counts.get("queued", 0) >= 1
