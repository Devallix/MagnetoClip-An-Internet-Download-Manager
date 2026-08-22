from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class Events:
    """Canonical event names used across the application."""

    DOWNLOAD_ADDED = "download.added"
    DOWNLOAD_UPDATED = "download.updated"
    DOWNLOAD_REMOVED = "download.removed"
    DOWNLOAD_STATE_CHANGED = "download.state_changed"
    PROGRESS_UPDATED = "download.progress"
    SPEED_UPDATED = "download.speed"
    CONNECTIONS_UPDATED = "download.connections"
    CATEGORY_ADDED = "category.added"
    CATEGORY_UPDATED = "category.updated"
    CATEGORY_REMOVED = "category.removed"
    CATEGORIES_CHANGED = "categories.changed"
    SETTINGS_CHANGED = "settings.changed"
    NETWORK_CHANGED = "network.changed"
    BROWSER_EVENT = "browser.event"
    BROWSER_STATUS_CHANGED = "browser.status_changed"
    PROXIES_CHANGED = "proxies.changed"
    ANALYTICS_REFRESHED = "analytics.refreshed"
    MEDIA_DETECTED = "media.detected"
    NOTIFICATION_REQUESTED = "notification.requested"
    UPDATE_AVAILABLE = "update.available"


class EventBus(QObject):
    """Thread-safe publish/subscribe event bus.

    Emissions flow through a Qt signal, so a ``post`` from any thread is
    delivered on the Qt event loop thread (queued connection), making it safe
    to publish from download worker threads and consume from the UI.
    """

    emitted = Signal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.emitted.connect(self._dispatch)

    def post(self, name: str, payload: Any = None) -> None:
        self.emitted.emit(name, payload)

    def connect(self, name: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        """Register ``handler`` for ``name``; returns a disconnect callable."""
        self._handlers.setdefault(name, []).append(handler)
        return lambda: self.disconnect(name, handler)

    def disconnect(self, name: str, handler: Callable[[Any], None]) -> None:
        handlers = self._handlers.get(name, [])
        if handler in handlers:
            handlers.remove(handler)

    def _dispatch(self, name: str, payload: Any) -> None:
        for handler in list(self._handlers.get(name, [])):
            handler(payload)
