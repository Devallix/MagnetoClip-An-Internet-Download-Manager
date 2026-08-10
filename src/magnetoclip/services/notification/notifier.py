"""System tray notifications for download lifecycle events."""

from __future__ import annotations

from magnetoclip.core.events.bus import Events
from magnetoclip.services.logging import get_logger

log = get_logger(__name__)


class Notifier:
    """Consumes ``NOTIFICATION_REQUESTED`` events and shows tray messages.

    Falls back to a no-op when no tray icon is available (e.g. headless tests
    or the browser-host process), so the service stays safe everywhere.
    """

    def __init__(self, context, tray=None) -> None:
        self.context = context
        self._tray = tray
        self._disconnect = context.events.connect(
            Events.NOTIFICATION_REQUESTED, self._on_notification
        )

    def attach_tray(self, tray) -> None:
        self._tray = tray

    def close(self) -> None:
        self._disconnect()

    def _on_notification(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        title = str(payload.get("title") or "MagnetoClip")
        body = str(payload.get("body") or "")
        kind = payload.get("kind")
        download_id = payload.get("download_id")
        log.info("notification", kind=kind, title=title)
        if kind == "completed":
            self._play_completion_sound()
        tray = self._tray
        if tray is None or not getattr(tray, "is_available", lambda: False)():
            return
        try:
            tray.show_message(title, body, download_id)
        except Exception:
            log.exception("notification_failed", title=title)

    @staticmethod
    def _play_completion_sound() -> None:
        """Play a short audible cue that a download finished (non-blocking)."""
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except ImportError:
            try:
                from PySide6.QtWidgets import QApplication

                QApplication.beep()
            except Exception:  # noqa: BLE001 - sound is best-effort
                log.debug("qt_beep_unavailable")
        except Exception:  # noqa: BLE001 - sound is best-effort
            log.warning("notification_sound_failed")
