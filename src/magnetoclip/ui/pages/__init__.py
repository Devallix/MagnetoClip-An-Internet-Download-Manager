"""Application pages."""

from __future__ import annotations

from .analytics import AnalyticsPage
from .browser import BrowserPage
from .detected import DetectedPage
from .downloads import DownloadsPage
from .overview import OverviewPage
from .pause import PausePage
from .settings import SettingsPage
from .speedtest import SpeedTestPage
from .torrents import TorrentsPage

__all__ = [
    "AnalyticsPage",
    "BrowserPage",
    "DetectedPage",
    "DownloadsPage",
    "OverviewPage",
    "PausePage",
    "SettingsPage",
    "SpeedTestPage",
    "TorrentsPage",
]