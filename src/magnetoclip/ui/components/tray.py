"""System tray icon with quick actions."""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from magnetoclip.core.events.bus import Events
from magnetoclip.resources import app_icon

from ..util import reveal_path


class SystemTray:
    """Tray icon showing active count/speed with pause/resume/exit actions."""

    def __init__(self, context, parent=None) -> None:
        self.context = context
        self._available = QSystemTrayIcon.isSystemTrayAvailable()
        self._message_download_id: int | None = None
        self._message_action: str | None = None
        self._action_callbacks: dict[str, callable] = {}
        self._remote_callback = None
        self._icon = QSystemTrayIcon(parent)
        self._icon.setIcon(app_icon())
        self._icon.setToolTip("MagnetoClip")
        self._icon.messageClicked.connect(self._on_message_clicked)
        self._build_menu()

    def is_available(self) -> bool:
        return self._available

    def _build_menu(self) -> None:
        menu = QMenu()
        self.open_action = QAction("Open MagnetoClip")
        self.open_action.triggered.connect(self._open_window)
        menu.addAction(self.open_action)
        menu.addSeparator()
        self.pause_action = QAction("Pause All")
        self.pause_action.triggered.connect(lambda: self._for_each("pause"))
        self.resume_action = QAction("Resume All")
        self.resume_action.triggered.connect(lambda: self._for_each("resume"))
        menu.addAction(self.pause_action)
        menu.addAction(self.resume_action)
        self.remote_action = QAction("Open Remote…")
        self.remote_action.triggered.connect(self._trigger_remote)
        menu.addAction(self.remote_action)
        self.license_action = QAction("License…")
        self.license_action.triggered.connect(self._trigger_license)
        menu.addAction(self.license_action)
        menu.addSeparator()
        self.exit_action = QAction("Exit")
        self.exit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(self.exit_action)
        self._icon.setContextMenu(menu)

    def show(self) -> None:
        if self._available:
            self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def set_status(self, active: int, speed_text: str) -> None:
        if self._available:
            self._icon.setToolTip(f"MagnetoClip — {active} active · {speed_text}")

    def show_message(
        self,
        title: str,
        body: str,
        download_id: int | None = None,
        action: str | None = None,
    ) -> None:
        if self._available:
            self._message_download_id = download_id
            self._message_action = action
            self._icon.showMessage(title, body, QSystemTrayIcon.Information, 6000)

    def register_action(self, action: str, callback) -> None:
        self._action_callbacks[action] = callback

    def set_remote_callback(self, callback) -> None:
        self._remote_callback = callback

    def set_license_callback(self, callback) -> None:
        self._license_callback = callback

    def _trigger_remote(self) -> None:
        if self._remote_callback is not None:
            self._remote_callback()

    def _trigger_license(self) -> None:
        if self._license_callback is not None:
            self._license_callback()

    def _on_message_clicked(self) -> None:
        action = self._message_action
        download_id = self._message_download_id
        self._message_action = None
        self._message_download_id = None
        if action is not None:
            callback = self._action_callbacks.get(action)
            if callback is not None:
                callback()
                return
            self._open_window()
            return
        if download_id is None:
            return
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        path = manager.path_of(download_id)
        if path is None:
            return
        reveal_path(path)

    def set_open_callback(self, callback) -> None:
        self._open_window = callback

    def _for_each(self, action: str) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        snapshots = manager.list_snapshots(limit=2000)
        ids = [
            s["id"]
            for s in snapshots
            if s["status"] in ("connecting", "downloading", "retrying", "verifying", "paused")
        ]
        for download_id in ids:
            getattr(manager, action)(download_id)
        self.context.events.post(Events.DOWNLOAD_UPDATED, {})

    def _open_window(self) -> None:
        parent = self._icon.parent()
        if parent is not None:
            parent.show()
            parent.raise_()
            parent.activateWindow()
