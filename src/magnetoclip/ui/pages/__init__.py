"""Application pages."""

from __future__ import annotations

from .analytics import AnalyticsPage
from .browser import BrowserPage
from .downloads import DownloadsPage
from .overview import OverviewPage
from .queue import QueuePage
from .scheduler import SchedulerPage
from .settings import SettingsPage

__all__ = [
    "AnalyticsPage",
    "BrowserPage",
    "DownloadsPage",
    "OverviewPage",
    "QueuePage",
    "SchedulerPage",
    "SettingsPage",
]
