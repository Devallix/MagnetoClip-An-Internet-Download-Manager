"""Persistent download statistics collection."""

from __future__ import annotations

import time

from sqlalchemy.orm import sessionmaker

from magnetoclip.core.events.bus import Events
from magnetoclip.database.models import DownloadStatistic
from magnetoclip.services.logging import get_logger

log = get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 5.0


class StatsCollector:
    """Samples live speed/connection events into ``download_statistics``.

    Writes are throttled per download (default every 5 seconds) so the table
    stays compact while still giving the dashboard a useful speed history.
    """

    def __init__(self, context, interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
        self.context = context
        self.session_factory: sessionmaker = context.session_factory
        self.interval = max(1.0, float(interval))
        self._last_write: dict[int, float] = {}
        self._pending: dict[int, dict] = {}
        self._connect()

    def _connect(self) -> None:
        events = self.context.events
        self._disconnect_speed = events.connect(Events.SPEED_UPDATED, self._on_speed)
        self._disconnect_connections = events.connect(
            Events.CONNECTIONS_UPDATED, self._on_connections
        )

    def close(self) -> None:
        for disconnect in (self._disconnect_speed, self._disconnect_connections):
            disconnect()

    # ----- event handlers -----

    def _on_speed(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        download_id = payload.get("id")
        if download_id is None:
            return
        self._pending.setdefault(download_id, {})["speed"] = float(payload.get("speed") or 0.0)
        self._maybe_write(download_id)

    def _on_connections(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        download_id = payload.get("id")
        if download_id is None:
            return
        self._pending.setdefault(download_id, {})["connections"] = int(payload.get("active") or 0)
        self._maybe_write(download_id)

    # ----- persistence -----

    def _maybe_write(self, download_id: int) -> None:
        now = time.monotonic()
        if now - self._last_write.get(download_id, 0.0) < self.interval:
            return
        self._last_write[download_id] = now
        data = self._pending.pop(download_id, {})
        if not data:
            return
        self.record(download_id, **data)

    def record(self, download_id: int, *, speed: float = 0.0, connections: int = 0) -> None:
        """Insert one statistics sample (also used by tests)."""
        with self.session_factory() as session:
            session.add(
                DownloadStatistic(
                    download_id=download_id,
                    speed=float(speed),
                    connections=int(connections),
                    bandwidth_used=int(speed * self.interval),
                )
            )
            session.commit()

    def flush(self) -> None:
        for download_id in list(self._pending):
            data = self._pending.pop(download_id, {})
            if data:
                self.record(download_id, **data)
