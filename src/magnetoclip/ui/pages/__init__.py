"""Application pages."""

from __future__ import annotations

from .analytics import AnalyticsPage
from .browser import BrowserPage
from .detected import DetectedPage
from .downloads import DownloadsPage
from .overview import OverviewPage
from .settings import SettingsPage
from .torrents import TorrentsPage

__all__ = [
    "AnalyticsPage",
    "BrowserPage",
    "DetectedPage",
    "DownloadsPage",
    "OverviewPage",
    "SettingsPage",
    "TorrentsPage",
]
