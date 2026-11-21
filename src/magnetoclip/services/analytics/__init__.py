"""Analytics: live statistics collection and dashboard aggregations."""

from .collector import StatsCollector
from .dashboard import DashboardService

__all__ = ["DashboardService", "StatsCollector"]


class AnalyticsService:
    """Bundles the collector (write side) and dashboard (read side)."""

    def __init__(self, context) -> None:
        self.collector = StatsCollector(context)
        self.dashboard = DashboardService(context)

    def close(self) -> None:
        self.collector.flush()
        self.collector.close()
