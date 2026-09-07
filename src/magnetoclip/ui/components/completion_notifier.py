"""In-window completion notification for finished downloads.

Tray balloons and Windows toasts are easy for Windows to suppress, so a quiet
dialog that holds long enough to be noticed is a reliable fallback. This widget
subscribes to ``NOTIFICATION_REQUESTED`` and pops a small, slide-in dialog at
the bottom-right corner of the main window when a download completes.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
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


class CompletionNotifier(QDialog):
    """A small sliding dialog that appears when a download completes.

    The dialog never takes keyboard focus away from what the user is doing
    (``WA_ShowWithoutActivating``) and dismisses itself after a few seconds or
    on click, so it reads as a notification rather than a blocking prompt.
    """

    def __init__(self, context, parent=None) -> None:
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint)
        self.context = context
        self._download_id: int | None = None
        self._save_path: str | None = None

        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlag(Qt.Tool, True)
        self.setFixedWidth(340)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        card = QWidget(self)
        card.setObjectName("completion_card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(6)

        title_row = QHBoxLayout()
        self.title_label = QLabel("Download complete")
        self.title_label.setObjectName("card_title")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        card_layout.addLayout(title_row)

        self.body_label = QLabel()
        self.body_label.setObjectName("card_caption")
        self.body_label.setWordWrap(True)
        card_layout.addWidget(self.body_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.open_button = AccentButton("Open folder")
        self.open_button.clicked.connect(self._open_folder)
        buttons.addWidget(self.open_button)
        close_button = QPushButton("Dismiss")
        close_button.clicked.connect(self._dismiss)
        buttons.addWidget(close_button)
        card_layout.addLayout(buttons)

        self._disconnect = context.events.connect(
            Events.NOTIFICATION_REQUESTED, self._on_notification
        )
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._dismiss)

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
        self._download_id = payload.get("download_id")
        self._save_path = payload.get("save_path")
        self.title_label.setText(title)
        self.body_label.setText(body)
        self._present()

    def _present(self) -> None:
        parent = self.parentWidget()
        if parent is None or not parent.isVisible():
            return
        parent_rect = parent.rect()
        x = parent_rect.right() - self.width() - 16
        edge_max = max(parent_rect.bottom(), parent_rect.height())
        target = QPoint(x, edge_max - self.height() - 48)
        start = QPoint(x, edge_max)
        self.move(parent.mapToGlobal(start))
        self.show()
        self.raise_()
        self._animate_from(start, target)
        self._hide_timer.start(_VISIBLE_MS)

    def _animate_from(self, start: QPoint, target: QPoint) -> None:
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(220)
        anim.setStartValue(self.parentWidget().mapToGlobal(start))
        anim.setEndValue(self.parentWidget().mapToGlobal(target))
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim = anim
        anim.start()

    def _open_folder(self) -> None:
        if not self._save_path:
            self._dismiss()
            return
        import subprocess
        import sys

        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer.exe", "/select,", self._save_path])
            else:
                subprocess.Popen(["xdg-open", self._save_path])
        except Exception:  # noqa: BLE001 - best-effort
            log.warning("open_folder_failed")
        self._dismiss()

    def _dismiss(self) -> None:
        self._hide_timer.stop()
        self.hide()
