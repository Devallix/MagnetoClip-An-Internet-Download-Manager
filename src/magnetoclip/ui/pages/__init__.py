"""Application pages."""

from __future__ import annotations

from .analytics import AnalyticsPage
from .browser import BrowserPage
from .detected import DetectedPage
from .downloads import DownloadsPage
from .overview import OverviewPage
from .queue import QueuePage
from .scheduler import SchedulerPage
from .settings import SettingsPage
from .torrents import TorrentsPage

__all__ = [
    "AnalyticsPage",
    "BrowserPage",
    "DetectedPage",
    "DownloadsPage",
    "OverviewPage",
    "QueuePage",
    "SchedulerPage",
    "SettingsPage",
    "TorrentsPage",
]
