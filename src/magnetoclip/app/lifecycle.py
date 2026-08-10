from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile

from magnetoclip.config.settings import Settings
from magnetoclip.core.categories.manager import CategoryManager
from magnetoclip.core.downloads.manager import DownloadManager
from magnetoclip.core.events.bus import EventBus
from magnetoclip.core.proxies.manager import ProxyManager
from magnetoclip.core.queues.manager import QueueManager
from magnetoclip.core.scheduler.scheduler import Scheduler
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
    queues = QueueManager(context)
    proxies = ProxyManager(context)
    context.categories = categories
    context.queues = queues
    context.proxies = proxies

    manager = DownloadManager(context)
    context.manager = manager
    queues.attach(manager)

    context.scheduler = Scheduler(context)

    from magnetoclip.services.analytics import AnalyticsService
    from magnetoclip.services.notification.notifier import Notifier

    context.analytics = AnalyticsService(context)
    context.notifier = Notifier(context)

    from magnetoclip.browser.service import BrowserIntegrationService

    context.browser = BrowserIntegrationService(context)
    return context


def acquire_single_instance_lock(data_dir: Path) -> QLockFile | None:
    """Ensure only one MagnetoClip instance runs; returns None if busy."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data_dir / "magnetoclip.lock"))
    if not lock.tryLock(100):
        return None
    return lock
