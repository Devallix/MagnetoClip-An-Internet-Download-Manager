from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile

from magnetoclip.config.settings import Settings
from magnetoclip.core.categories.manager import CategoryManager
from magnetoclip.core.downloads.manager import DownloadManager
from magnetoclip.core.events.bus import EventBus
from magnetoclip.core.proxies.manager import ProxyManager
from magnetoclip.database.repositories import SettingsStore
from magnetoclip.database.session import Database
from magnetoclip.services.filesystem.paths import (
    default_config_dir,
    default_data_dir,
    default_log_dir,
    ensure_dirs,
)
from magnetoclip.services.logging.setup import configure_logging

from .context import AppContext


def build_context(
    *,
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    log_dir: Path | None = None,
    log_level: str = "INFO",
) -> AppContext:
    """Create the application context: paths, logging, database, settings, events."""
    config_dir = config_dir or default_config_dir()
    data_dir = data_dir or default_data_dir()
    log_dir = log_dir or default_log_dir()

    ensure_dirs(config_dir, data_dir, log_dir)
    configure_logging(log_dir, level=log_level)

    database = Database(data_dir / "magnetoclip.db")
    database.initialize()

    store = SettingsStore(database.Session)
    settings = Settings.from_store(store.load_all())
    settings.set("advanced.log_level", log_level.lower())

    events = EventBus()

    context = AppContext(
        settings=settings,
        events=events,
        database=database,
        config_dir=config_dir,
        data_dir=data_dir,
        log_dir=log_dir,
    )

    categories = CategoryManager(context)
    proxies = ProxyManager(context)
    context.categories = categories
    context.proxies = proxies

    manager = DownloadManager(context)
    context.manager = manager

    from magnetoclip.services.analytics import AnalyticsService
    from magnetoclip.services.notification.notifier import Notifier

    context.analytics = AnalyticsService(context)
    context.notifier = Notifier(context)

    from magnetoclip.browser.service import BrowserIntegrationService

    context.browser = BrowserIntegrationService(context)

    from magnetoclip.torrent.client import ClientConfig, TorrentClient, available as torrent_available

    if torrent_available():
        try:
            cfg = ClientConfig(
                listen_port=int(settings.get("torrent.listen_port", 6881)),
                enable_dht=bool(settings.get("torrent.enable_dht", True)),
                enable_pex=bool(settings.get("torrent.enable_pex", True)),
                enable_encryption=bool(settings.get("torrent.enable_encryption", True)),
                max_connections=int(settings.get("torrent.max_connections", 200)),
                max_uploads=int(settings.get("torrent.max_uploads", 4)),
            )
            context.torrent_client = TorrentClient(cfg)
        except Exception:
            context.torrent_client = None

    return context


def acquire_single_instance_lock(data_dir: Path) -> QLockFile | None:
    """Ensure only one MagnetoClip instance runs; returns None if busy."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data_dir / "magnetoclip.lock"))
    if not lock.tryLock(100):
        return None
    return lock
