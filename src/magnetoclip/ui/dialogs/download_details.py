"""Details dialog for a single download, opened by double-clicking a row."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events

from ..components.buttons import IconToolButton
from ..components.download_card import BADGE_LABELS
from ..components.icons import tool_icon, type_icon
from ..components.progress import StyledProgressBar
from ..util import format_bytes, format_eta, format_speed, fraction

ACTIVE = {"connecting", "downloading", "retrying", "verifying"}
TERMINAL = {"completed", "failed", "verification_failed", "stopped"}

_FIELDS = (
    ("name", "Name"),
    ("size", "Size"),
    ("status", "Status"),
    ("speed", "Speed"),
    ("time_left", "Time left"),
    ("added", "Time added"),
)


def _format_added(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


class DownloadDetailsDialog(QDialog):
    """Shows download metadata with progress and start/pause controls."""

    def __init__(self, context, snapshot: dict, parent=None) -> None:
        super().__init__(parent)
        self.context = context
        self.setObjectName("details_dialog")
        self.setWindowTitle("Download details")
        self.setMinimumWidth(560)

        self._id = int(snapshot["id"])
        self._last_status = snapshot.get("status") or "queued"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        icon_label = QLabel()
        icon_label.setPixmap(
            type_icon(snapshot.get("detected_type")).pixmap(32, 32)
        )
        icon_label.setFixedSize(32, 32)
        title_row.addWidget(icon_label)
        self.title_label = QLabel()
        self.title_label.setObjectName("card_title")
        self.title_label.setStyleSheet("font-weight: 700;")
        title_row.addWidget(self.title_label, 1)
        layout.addLayout(title_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        self._values: dict[str, QLabel] = {}
        for row, (key, label) in enumerate(_FIELDS):
            caption = QLabel(label)
            caption.setObjectName("detail_label")
            value = QLabel()
            value.setObjectName("detail_value")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(value, row, 1)
            self._values[key] = value
        layout.addLayout(grid)

        self.progress = StyledProgressBar()
        self.progress.setObjectName("status_bar")
        layout.addWidget(self.progress)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.start_button = IconToolButton(tool_icon("start"), "Start")
        self.pause_button = IconToolButton(tool_icon("pause"), "Pause")
        self.start_button.clicked.connect(self._start)
        self.pause_button.clicked.connect(self._pause)
        controls.addWidget(self.start_button)
        controls.addWidget(self.pause_button)
        controls.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        events = context.events
        self._disconnect_updated = events.connect(Events.DOWNLOAD_UPDATED, self._refresh)
        self._disconnect_progress = events.connect(Events.PROGRESS_UPDATED, self._refresh)
        self.finished.connect(self._disconnect)

        self._refresh(snapshot)

    def _disconnect(self) -> None:
        for disconnect in (self._disconnect_updated, self._disconnect_progress):
            if disconnect is not None:
                disconnect()

    def _snapshot(self, _payload: Any = None) -> dict | None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return None
        download = manager.get_download(self._id)
        if download is None:
            return None
        return manager.snapshot_item(download)

    def _refresh(self, payload: Any = None) -> None:
        snapshot = self._snapshot(payload)
        if snapshot is None:
            return
        self._last_status = snapshot.get("status") or "queued"
        status = self._last_status
        downloaded = snapshot.get("size_downloaded") or 0
        total = snapshot.get("size_total")
        speed = snapshot.get("speed")

        self.title_label.setText(snapshot.get("filename") or "Untitled")
        self._values["name"].setText(snapshot.get("filename") or "Untitled")
        if total:
            self._values["size"].setText(
                f"{format_bytes(downloaded)} / {format_bytes(total)}"
            )
        else:
            self._values["size"].setText(format_bytes(downloaded))
        self._values["status"].setText(BADGE_LABELS.get(status, status.title()))

        if status in ACTIVE:
            self._values["speed"].setText(format_speed(speed))
            eta = snapshot.get("eta_seconds")
            if eta is None and total and speed:
                eta = (total - downloaded) / speed
            self._values["time_left"].setText(format_eta(eta))
        elif status == "completed":
            self._values["speed"].setText("Done")
            self._values["time_left"].setText("—")
        elif status in ("failed", "verification_failed"):
            error = snapshot.get("error") or "Error"
            self._values["speed"].setText(
                error if len(error) <= 40 else error[:40] + "…"
            )
            self._values["time_left"].setText("—")
        else:
            self._values["speed"].setText("—")
            self._values["time_left"].setText("—")
        self._values["added"].setText(_format_added(snapshot.get("created_at")))

        bar_state = "downloading"
        if status == "paused":
            bar_state = "paused"
        elif status in TERMINAL:
            bar_state = status
        self.progress.set_state(bar_state)
        self.progress.set_fraction(fraction(downloaded, total))

        can_start = status in (
            "queued", "scheduled", "paused", "failed",
            "stopped", "verification_failed",
        )
        self.start_button.setEnabled(can_start)
        self.pause_button.setEnabled(status in ACTIVE)

    def _start(self) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        if self._last_status == "paused":
            manager.resume(self._id)
        else:
            manager.start(self._id)
        self._refresh()

    def _pause(self) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is not None:
            manager.pause(self._id)
        self._refresh()
