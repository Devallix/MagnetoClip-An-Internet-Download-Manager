"""Overview dashboard: headline stats and recent activity."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events

from ..components.download_card import DownloadCard
from ..components.stat_card import StatCard
from ..util import format_bytes, format_speed
from .base import Page, make_scrollable

TERMINAL = {"completed", "failed", "verification_failed", "stopped"}

_ACCENT = {
    "active": "#3B82F6",
    "completed": "#34D399",
    "bytes": "#22D3EE",
    "speed": "#8B5CF6",
}


class _ActionCard(QFrame):
    """A small clickable card used for quick-action buttons."""

    def __init__(self, title: str, hint: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("overview_action")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)

        t = QLabel(title)
        t.setObjectName("overview_action_title")
        t.setStyleSheet("font-size: 14px;")
        h = QLabel(hint)
        h.setObjectName("overview_action_hint")
        layout.addWidget(t)
        layout.addWidget(h)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and hasattr(self, "_on_click"):
            self._on_click()
        super().mousePressEvent(event)


class OverviewPage(Page):
    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)

        layout = make_scrollable(self, spacing=20)
        container = layout.parentWidget()
        if container is not None:
            container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        # ── page header ──────────────────────────────────────────────────────
        header = QVBoxLayout()
        title = QLabel("Overview")
        title.setObjectName("page_title")
        subtitle = QLabel("Your download activity at a glance")
        subtitle.setObjectName("page_subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        # ── hero stat cards (2×2) ───────────────────────────────────────────
        grid = QGridLayout()
        grid.setSpacing(12)

        self.active_card = StatCard("Active downloads")
        self.active_card.set_accent(_ACCENT["active"])
        self.active_card.setMinimumHeight(100)

        self.completed_card = StatCard("Completed")
        self.completed_card.set_accent(_ACCENT["completed"])
        self.completed_card.setMinimumHeight(100)

        self.bytes_card = StatCard("Total downloaded")
        self.bytes_card.set_accent(_ACCENT["bytes"])
        self.bytes_card.setMinimumHeight(100)

        self.speed_card = StatCard("Current speed")
        self.speed_card.set_accent(_ACCENT["speed"])
        self.speed_card.setMinimumHeight(100)

        grid.addWidget(self.active_card, 0, 0)
        grid.addWidget(self.completed_card, 0, 1)
        grid.addWidget(self.bytes_card, 1, 0)
        grid.addWidget(self.speed_card, 1, 1)
        layout.addLayout(grid)

        # ── recent activity section card ─────────────────────────────────────
        activity_frame = QFrame()
        activity_frame.setObjectName("card")
        activity_frame.setMaximumHeight(300)
        activity_inner = QVBoxLayout(activity_frame)
        activity_inner.setContentsMargins(16, 14, 16, 14)
        activity_inner.setSpacing(8)

        activity_title = QLabel("Recent Activity")
        activity_title.setObjectName("card_title")
        activity_inner.addWidget(activity_title)

        self.recent_cards: dict[int, DownloadCard] = {}
        self.recent_layout = QVBoxLayout()
        self.recent_layout.setSpacing(8)
        activity_inner.addLayout(self.recent_layout)

        self.empty_label = QLabel(
            "No recent activity yet. Start a download to see it appear here."
        )
        self.empty_label.setObjectName("overview_empty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        activity_inner.addWidget(self.empty_label)

        layout.addWidget(activity_frame)

        # ── quick actions ────────────────────────────────────────────────────
        actions_label = QLabel("Quick Actions")
        actions_label.setObjectName("card_title")
        layout.addWidget(actions_label)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(12)

        new_download_card = _ActionCard(
            "New Download", "Paste a URL to start downloading"
        )
        new_download_card._on_click = lambda: self._navigate("downloads")
        actions_row.addWidget(new_download_card)

        view_analytics_card = _ActionCard(
            "View Analytics", "See detailed download statistics"
        )
        view_analytics_card._on_click = lambda: self._navigate("analytics")
        actions_row.addWidget(view_analytics_card)

        view_torrents_card = _ActionCard(
            "Torrents", "Manage your torrent downloads"
        )
        view_torrents_card._on_click = lambda: self._navigate("torrents")
        actions_row.addWidget(view_torrents_card)

        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        events = context.events
        events.connect(Events.DOWNLOAD_UPDATED, self._on_updated)
        events.connect(Events.PROGRESS_UPDATED, self._on_progress)
        events.connect(Events.SPEED_UPDATED, self._on_speed)

        self.refresh()

    # ── navigation ───────────────────────────────────────────────────────────

    def _navigate(self, page_key: str) -> None:
        """Ask the main window to switch to *page_key*."""
        window = self.window()
        activate = getattr(window, "_activate_nav", None)
        if activate is not None:
            activate(page_key)

    # ── event handlers ───────────────────────────────────────────────────────

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
            card.setMaximumHeight(90)
            self.recent_layout.insertWidget(0, card)
            self.recent_cards[snapshot["id"]] = card
        card.update_snapshot(snapshot)
        while len(self.recent_cards) > 5:
            oldest_id = next(iter(self.recent_cards))
            oldest = self.recent_cards.pop(oldest_id)
            self.recent_layout.removeWidget(oldest)
            oldest.deleteLater()
        self.empty_label.setVisible(len(self.recent_cards) == 0)

    def refresh(self) -> None:
        manager = getattr(self.context, "manager", None)
        if manager is None:
            return
        snapshots = manager.list_snapshots(limit=2000)
        active = sum(1 for s in snapshots if s["status"] not in TERMINAL)
        completed = sum(1 for s in snapshots if s["status"] == "completed")
        total_bytes = sum(
            s.get("size_downloaded") or 0
            for s in snapshots
            if s["status"] == "completed"
        )
        self.active_card.set_value(str(active))
        self.completed_card.set_value(str(completed))
        self.bytes_card.set_value(format_bytes(total_bytes))
        self.empty_label.setVisible(len(self.recent_cards) == 0)
