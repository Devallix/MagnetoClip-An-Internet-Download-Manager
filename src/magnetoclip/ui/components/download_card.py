"""A single download row shown on the Downloads / Completed pages."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..util import format_bytes, format_eta, format_speed, fraction
from .progress import StyledProgressBar

BADGE_LABELS = {
    "queued": "Queued",
    "scheduled": "Scheduled",
    "connecting": "Connecting",
    "downloading": "Downloading",
    "paused": "Paused",
    "retrying": "Retrying",
    "verifying": "Verifying",
    "completed": "Completed",
    "failed": "Failed",
    "verification_failed": "Verification failed",
    "stopped": "Stopped",
}

ACTIVE = {"connecting", "downloading", "retrying", "verifying"}


class DownloadCard(QFrame):
    pause_requested = Signal(int)
    resume_requested = Signal(int)
    cancel_requested = Signal(int)
    remove_requested = Signal(int)
    priority_changed = Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.download_id: int | None = None
        self._status = "queued"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.name_label = QLabel()
        self.name_label.setObjectName("card_title")
        self.name_label.setStyleSheet("font-weight: 700;")
        self.url_label = QLabel()
        self.url_label.setObjectName("card_caption")
        titles.addWidget(self.name_label)
        titles.addWidget(self.url_label)
        top.addLayout(titles, 1)

        self.badge = QLabel()
        self.badge.setObjectName("badge")
        top.addWidget(self.badge, 0, Qt.AlignTop)

        self.priority_label = QLabel()
        self.priority_label.setObjectName("card_caption")
        top.addWidget(self.priority_label, 0, Qt.AlignTop)

        self._menu_holder = QHBoxLayout()
        self._menu_holder.setSpacing(6)
        top.addLayout(self._menu_holder)

        layout.addLayout(top)

        self.progress = StyledProgressBar()
        layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        self.size_label = QLabel()
        self.size_label.setObjectName("card_caption")
        self.speed_label = QLabel()
        self.speed_label.setObjectName("card_caption")
        self.eta_label = QLabel()
        self.eta_label.setObjectName("card_caption")
        bottom.addWidget(self.size_label)
        bottom.addStretch(1)
        bottom.addWidget(self.speed_label)
        bottom.addWidget(self.eta_label)
        layout.addLayout(bottom)

    def _make_button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("role", "ghost")
        button.setCursor(Qt.PointingHandCursor)
        self._menu_holder.addWidget(button)
        return button

    def configure(
        self,
        *,
        can_pause: bool,
        can_resume: bool,
        can_cancel: bool,
        can_remove: bool,
        can_change_priority: bool = False,
    ) -> None:
        """Build the action buttons for this card's page context."""
        while self._menu_holder.count():
            item = self._menu_holder.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if can_change_priority:
            for delta, tip in ((-1, "Lower priority"), (1, "Raise priority")):
                button = self._make_button("▲" if delta > 0 else "▼")
                button.setToolTip(tip)
                button.clicked.connect(
                    lambda checked=False, d=delta: self.priority_changed.emit(
                        self.download_id, d
                    )
                )

        if can_pause:
            button = self._make_button("Pause")
            button.clicked.connect(lambda: self.pause_requested.emit(self.download_id))
        if can_resume:
            button = self._make_button("Resume")
            button.clicked.connect(lambda: self.resume_requested.emit(self.download_id))
        if can_cancel:
            button = self._make_button("Cancel")
            button.clicked.connect(lambda: self.cancel_requested.emit(self.download_id))
        if can_remove:
            button = self._make_button("Remove")
            button.setProperty("role", "danger")
            button.clicked.connect(lambda: self.remove_requested.emit(self.download_id))

    def update_snapshot(self, snapshot: dict) -> None:
        self.download_id = snapshot["id"]
        self.name_label.setText(snapshot.get("filename") or "Untitled")
        url = snapshot.get("url") or ""
        self.url_label.setText(url if len(url) <= 90 else url[:90] + "…")

        status = snapshot.get("status") or "queued"
        self._status = status
        self.badge.setText(BADGE_LABELS.get(status, status.title()))
        self.badge.setProperty("state", status if status != "verification_failed" else "failed")

        priority = snapshot.get("priority")
        self.priority_label.setText(f"P{priority}" if priority else "")
        if priority:
            self.priority_label.setToolTip(f"Priority {priority}")

        downloaded = snapshot.get("size_downloaded") or 0
        total = snapshot.get("size_total")
        speed = snapshot.get("speed")
        self.progress.set_state(status if status in ("completed", "failed", "paused") else "idle")
        if total:
            self.progress.set_fraction(fraction(downloaded, total))
            self.size_label.setText(f"{format_bytes(downloaded)} / {format_bytes(total)}")
        else:
            self.progress.setValue(0)
            self.size_label.setText(format_bytes(downloaded))

        if status in ACTIVE:
            self.speed_label.setText(format_speed(speed))
            eta_seconds = snapshot.get("eta_seconds")
            if eta_seconds is None:
                remaining = (total - downloaded) if total else None
                eta_seconds = (remaining / speed) if (remaining and speed) else None
            self.eta_label.setText(f"ETA {format_eta(eta_seconds)}")
        elif status == "completed":
            self.speed_label.setText("Done")
            self.eta_label.setText("")
        elif status in ("failed", "verification_failed"):
            error = snapshot.get("error") or "Error"
            self.speed_label.setText(error if len(error) <= 40 else error[:40] + "…")
            self.eta_label.setText("")
        else:
            self.speed_label.setText("")
            self.eta_label.setText("")
