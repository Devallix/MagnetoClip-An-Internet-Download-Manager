"""System tray / Windows toast notifications for download lifecycle events."""

from __future__ import annotations

import sys

from magnetoclip.core.events.bus import Events
from magnetoclip.services.logging import get_logger
from magnetoclip.services.shell.commands import build_launch_uri

from . import win_toast

log = get_logger(__name__)


class Notifier:
    """Consumes ``NOTIFICATION_REQUESTED`` events and shows notifications.

    The main GUI enables native Windows toasts (``enable_toasts()``), which are
    preferred because Windows 10/11 often suppress QSystemTrayIcon balloons.
    With toasts disabled the notifier falls back to a tray balloon, and to a
    no-op when no tray icon is available (e.g. the browser-host process), so
    the service stays safe everywhere.
    """

    def __init__(self, context, tray=None, toast_enabled: bool = False) -> None:
        self.context = context
        self._tray = tray
        self._toast_enabled = toast_enabled
        self._disconnect = context.events.connect(
            Events.NOTIFICATION_REQUESTED, self._on_notification
        )

    def enable_toasts(self) -> None:
        """Prefer native Windows toasts over tray balloons from now on."""
        self._toast_enabled = True

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
        action = payload.get("action")
        log.info("notification", kind=kind, title=title)
        if kind == "completed":
            self._play_completion_sound()
        if self._toast_enabled and sys.platform == "win32":
            launch = build_launch_uri(kind, download_id, action)
            try:
                if win_toast.show_toast(title, body, launch=launch):
                    return
            except Exception:  # noqa: BLE001 - fall back to the tray balloon
                log.exception("toast_call_failed", title=title)
        tray = self._tray
        if tray is None or not getattr(tray, "is_available", lambda: False)():
            return
        try:
            tray.show_message(title, body, download_id, action)
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