"""Overview dashboard: headline stats and recent activity."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout

from magnetoclip.core.events.bus import Events

from ..components.download_card import DownloadCard
from ..components.stat_card import StatCard
from ..util import format_bytes, format_speed
from .base import Page, make_scrollable

TERMINAL = {"completed", "failed", "verification_failed", "stopped"}


class OverviewPage(Page):
    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)

        layout = make_scrollable(self, spacing=20)

        header = QVBoxLayout()
        title = QLabel("Overview")
        title.setObjectName("page_title")
        subtitle = QLabel("Your download activity at a glance")
        subtitle.setObjectName("page_subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        self.active_card = StatCard("Active downloads")
        self.completed_card = StatCard("Completed")
        self.bytes_card = StatCard("Total downloaded")
        self.speed_card = StatCard("Current speed")
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.addWidget(self.active_card, 0, 0)
        grid.addWidget(self.completed_card, 0, 1)
        grid.addWidget(self.bytes_card, 0, 2)
        grid.addWidget(self.speed_card, 0, 3)
        layout.addLayout(grid)

        recent_header = QLabel("Recent activity")
        recent_header.setObjectName("page_subtitle")
        layout.addWidget(recent_header)

        self.recent_cards: dict[int, DownloadCard] = {}
        self.recent_layout = QVBoxLayout()
        self.recent_layout.setSpacing(8)
        layout.addLayout(self.recent_layout)
        layout.addStretch(1)

        events = context.events
        events.connect(Events.DOWNLOAD_UPDATED, self._on_updated)
        events.connect(Events.PROGRESS_UPDATED, self._on_progress)
        events.connect(Events.SPEED_UPDATED, self._on_speed)

        self.refresh()

    def _on_updated(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("id"):
            self._upsert_recent(payload)
        self.refresh()

    def _on_progress(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        download = manager.get_download(payload["id"])
        if download is not None:
            self._upsert_recent(manager.snapshot_item(download))
        self.speed_card.set_value(format_speed(payload.get("speed")))

    def _on_speed(self, payload: Any) -> None:
        self.speed_card.set_value(format_speed(payload.get("speed")))

    def _upsert_recent(self, snapshot: dict) -> None:
        card = self.recent_cards.get(snapshot["id"])
        if card is None:
            card = DownloadCard()
            card.setMaximumHeight(150)
            self.recent_layout.insertWidget(0, card)
            self.recent_cards[snapshot["id"]] = card
        card.update_snapshot(snapshot)
        while len(self.recent_cards) > 5:
            oldest_id = next(iter(self.recent_cards))
            oldest = self.recent_cards.pop(oldest_id)
            self.recent_layout.removeWidget(oldest)
            oldest.deleteLater()

    def refresh(self) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        snapshots = manager.list_snapshots(limit=2000)
        active = sum(1 for s in snapshots if s["status"] not in TERMINAL)
        completed = sum(1 for s in snapshots if s["status"] == "completed")
        total_bytes = sum(s.get("size_downloaded") or 0 for s in snapshots
                          if s["status"] == "completed")
        self.active_card.set_value(str(active))
        self.completed_card.set_value(str(completed))
        self.bytes_card.set_value(format_bytes(total_bytes))
