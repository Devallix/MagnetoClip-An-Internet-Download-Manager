"""App-level toast notification for finished downloads.

Tray balloons and Windows toasts are easy for Windows to suppress, so an
app-owned toast is the reliable channel: a frameless card that slides in at
the bottom-center of the screen, above the taskbar, independent of the main
window. It appears even when the app window is minimized or unfocused, holds
long enough to be read, then slides away on its own or on click.

The toast is a top-level overlay window rather than a child of the main
window on purpose. Parenting it to the window ties its visibility to the
window's, so a minimized or tray-hidden app would silently eat the "done"
feedback and leave only the completion sound.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from magnetoclip.core.events.bus import Events
from magnetoclip.services.logging.setup import get_logger

from .buttons import AccentButton

log = get_logger(__name__)

_VISIBLE_MS = 6000
_FADE_MS = 200
_SLIDE_MS = 220
# Keep at most this many toasts on screen; the oldest is dropped if exceeded.
_MAX_VISIBLE = 3
# Wider card so filenames/paths have room; text wraps inside it.
_TOAST_WIDTH = 460
# Zero-width breaks are inserted after these so unbroken paths/filenames can
# wrap instead of forcing the card wider than its fixed width.
_WRAP_BREAK_AFTER = ("\\", "/", ".", "_", "-", ":")


def _soft_wrap(text: str) -> str:
    """Add zero-width break hints after path/name separators.

    ``QLabel`` word-wrap only breaks at whitespace, so a long path or filename
    with no spaces would overflow a fixed-width card. Inserting an invisible
    ``U+200B`` after separators lets the label break there while the rendered
    text stays identical.
    """
    if not text:
        return text
    out: list[str] = []
    for index, char in enumerate(text):
        out.append(char)
        if char in _WRAP_BREAK_AFTER and index + 1 < len(text):
            out.append("\u200b")
    return "".join(out)


class _Toast(QWidget):
    """One completion card shown as a top-level overlay window."""

    _WINDOW_FLAGS = (
        Qt.FramelessWindowHint
        | Qt.Tool
        | Qt.WindowStaysOnTopHint
        | Qt.WindowDoesNotAcceptFocus
    )

    def __init__(self, parent, settings=None) -> None:
        # Parented to the controller for lifetime only: the window-type flags
        # below keep it an independent, always-on-top desktop window, so it is
        # not hidden or clipped by the main window's own state.
        super().__init__(parent, self._WINDOW_FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(_TOAST_WIDTH)

        # Settings object (read at click time so a change of default folder is
        # honoured). The Open folder button targets the default download
        # directory configured under Settings → General.
        self._settings = settings

        # Set before the buttons are wired up: they connect to this callable,
        # and the owner replaces it with the real dismiss handler.
        self._save_path: str | None = None
        self.request_dismiss = lambda: None

        card = QWidget()
        card.setObjectName("completion_card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(6)

        # The card fills the whole toast window; without a top-level layout the
        # child card has no geometry and the toast only renders a fraction of
        # its content (and stacks at the wrong height).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.title_label = QLabel("Download complete")
        self.title_label.setObjectName("card_title")
        self.title_label.setWordWrap(True)
        card_layout.addWidget(self.title_label)

        self.body_label = QLabel()
        self.body_label.setObjectName("card_caption")
        self.body_label.setWordWrap(True)
        card_layout.addWidget(self.body_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.open_button = AccentButton("Open folder")
        self.open_button.clicked.connect(self._on_open_folder)
        buttons.addWidget(self.open_button)
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.clicked.connect(self._on_dismiss)
        buttons.addWidget(self.dismiss_button)
        card_layout.addLayout(buttons)

    def _on_dismiss(self) -> None:
        # Resolve at click time: ``request_dismiss`` is replaced by the owner
        # after construction, and connecting to it directly would freeze the
        # placeholder no-op that was bound when the button was created.
        self.request_dismiss()

    def configure(self, title: str, body: str, save_path: str | None) -> None:
        self.title_label.setText(_soft_wrap(title))
        self.body_label.setText(_soft_wrap(body))
        self._save_path = save_path

    def _on_open_folder(self) -> None:
        """Open the default download folder from Settings.

        Uses the folder configured as ``downloads.default_directory``
        (Settings → General → Default download folder). If none is configured,
        falls back to the finished file's own folder. This only opens the
        folder — it deliberately does not dismiss the toast, so the card stays
        put for any further action.
        """
        import os
        import subprocess
        import sys

        folder: str | None = None
        if self._settings is not None:
            configured = self._settings.get("downloads.default_directory", "")
            if configured:
                folder = str(configured)
        if not folder and self._save_path:
            folder = os.path.dirname(os.path.normpath(self._save_path))
        if not folder:
            return
        folder = os.path.normpath(folder)
        try:
            if sys.platform == "win32":
                # Plain folder argument opens the folder in Explorer.
                subprocess.Popen(["explorer.exe", folder])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:  # noqa: BLE001 - best-effort
            log.warning("open_folder_failed", path=folder)


class CompletionNotifier(QWidget):
    """Owns desktop-level completion toasts.

    Toasts stack upwards at the bottom-center of the screen the app window
    lives on, so several downloads finishing at once are each reported rather
    than overwriting one another.
    """

    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self._toasts: list[_Toast] = []

        # The notifier itself is only a controller and is never shown; the
        # toasts are top-level overlay windows positioned in screen space.
        # Hide explicitly: without it Qt still hit-tests this widget, and its
        # default 100x30 geometry sits exactly over the menu bar's first
        # actions, silently swallowing real mouse clicks on the Help menu.
        self.hide()
        self._disconnect = context.events.connect(
            Events.NOTIFICATION_REQUESTED, self._on_notification
        )

    def is_active(self) -> bool:
        """Report whether this controller owns completion notifications.

        ``True`` on every completion: the toast is a desktop overlay, so the
        main window does not need to be visible for it to appear.
        """
        return True

    def close(self) -> None:
        self._disconnect()
        super().close()

    def _on_notification(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("kind") != "completed":
            return
        title = str(payload.get("title") or "Download complete")
        body = str(payload.get("body") or "The file has finished downloading.")
        save_path = payload.get("save_path")

        while len(self._toasts) >= _MAX_VISIBLE:
            self._toasts[0].request_dismiss()

        toast = _Toast(self, settings=getattr(self.context, "settings", None))
        toast.configure(title, body, save_path)
        toast.adjustSize()
        toast.request_dismiss = lambda t=toast: self._dismiss(t)
        self._toasts.append(toast)
        self._reposition()
        toast.show()
        toast.raise_()
        self._slide_in(toast)

        timer = QTimer(toast)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda t=toast: self._dismiss(t))
        timer.start(_VISIBLE_MS)
        toast._hide_timer = timer

    # ----- geometry -----

    def _screen_geometry(self):
        """Available geometry (taskbar excluded) of the toast's screen."""
        parent = self.parentWidget()
        screen = getattr(parent, "screen", lambda: None)() if parent is not None else None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _reposition(self) -> None:
        if not self._toasts:
            return
        geom = self._screen_geometry()
        width = self._toasts[-1].width()
        x = geom.center().x() - width // 2
        y = geom.bottom() - 24
        for toast in reversed(self._toasts):
            y -= toast.height()
            toast._target_x = x
            toast._target_y = y
            y -= 8

    def _slide_in(self, toast: _Toast) -> None:
        geom = self._screen_geometry()
        start = QPoint(toast._target_x, geom.bottom())
        end = QPoint(toast._target_x, toast._target_y)
        toast.move(start)
        anim = QPropertyAnimation(toast, b"pos", toast)
        anim.setDuration(_SLIDE_MS)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        toast._anim = anim
        anim.start()

    def _dismiss(self, toast: _Toast) -> None:
        if toast not in self._toasts:
            return
        self._toasts.remove(toast)
        geom = self._screen_geometry()
        anim = QPropertyAnimation(toast, b"pos", toast)
        anim.setDuration(_SLIDE_MS)
        anim.setStartValue(toast.pos())
        anim.setEndValue(QPoint(toast.x(), geom.bottom()))
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(toast.deleteLater)
        anim.start()
        toast._anim = anim
        self._reposition()
        for other in self._toasts:
            self._slide_to(other, getattr(other, "_target_y", other.y()))
        if not self._toasts:
            self.hide()

    def _slide_to(self, toast: _Toast, target_y: int) -> None:
        anim = QPropertyAnimation(toast, b"pos", toast)
        anim.setDuration(_SLIDE_MS)
        anim.setStartValue(toast.pos())
        anim.setEndValue(
            QPoint(getattr(toast, "_target_x", toast.x()), target_y)
        )
        anim.setEasingCurve(QEasingCurve.OutCubic)
        toast._anim = anim
        anim.start()