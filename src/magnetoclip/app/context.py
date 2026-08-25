from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from magnetoclip.config.settings import Settings
from magnetoclip.core.events.bus import EventBus
from magnetoclip.database.session import Database
from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)


@dataclass
class AppContext:
    """Dependency-injection container wiring the application together."""

    settings: Settings
    events: EventBus
    database: Database
    config_dir: Path
    data_dir: Path
    log_dir: Path
    session_factory: sessionmaker[Session] = field(init=False)
    categories: object = None
    proxies: object = None
    manager: object = None
    analytics: object = None
    notifier: object = None
    browser: object = None
    torrent_client: object = None
    remote: object = None

    def __post_init__(self) -> None:
        self.session_factory = self.database.Session

    def new_session(self) -> Session:
        return self.session_factory()

    async def shutdown(self) -> None:
        remote = getattr(self, "remote", None)
        if remote is not None:
            try:
                await remote.stop()
            except Exception:
                log.warning("remote_server_shutdown_failed", exc_info=True)
        torrent_client = getattr(self, "torrent_client", None)
        if torrent_client is not None:
            try:
                await torrent_client.shutdown()
            except Exception:
                log.warning("torrent_client_shutdown_failed", exc_info=True)
        analytics = getattr(self, "analytics", None)
        if analytics is not None:
            try:
                analytics.close()
            except Exception:
                log.warning("analytics_close_failed", exc_info=True)
        notifier = getattr(self, "notifier", None)
        if notifier is not None:
            try:
                notifier.close()
            except Exception:
                log.warning("notifier_close_failed", exc_info=True)
        manager = getattr(self, "manager", None)
        if manager is not None:
            try:
                await manager.shutdown()
            except Exception:
                log.warning("manager_shutdown_failed", exc_info=True)
        self.database.close()
        from magnetoclip.services.logging.setup import shutdown_logging

        shutdown_logging()
