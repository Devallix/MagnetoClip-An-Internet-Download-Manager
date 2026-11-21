"""Analytics page: dashboard aggregations and simple charts."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from magnetoclip.core.events.bus import Events

from ..components.stat_card import StatCard
from ..util import format_bytes, format_speed
from ..widgets.chart import BarChart
from .base import Page, make_scrollable

DAYS = 14


class AnalyticsPage(Page):
    def __init__(self, context, parent=None) -> None:
        super().__init__(context, parent)
        self.context.events.connect(Events.ANALYTICS_REFRESHED, lambda _: self.refresh())
        self.context.events.connect(Events.DOWNLOAD_ADDED, lambda _: self.refresh())
        self.context.events.connect(Events.DOWNLOAD_REMOVED, lambda _: self.refresh())

        layout = make_scrollable(self)

        header = QVBoxLayout()
        title = QLabel("Analytics")
        title.setObjectName("page_title")
        subtitle = QLabel("Download statistics")
        subtitle.setObjectName("page_subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        self.total_card = StatCard("Total downloads")
        self.completed_card = StatCard("Completed")
        self.failed_card = StatCard("Failed")
        self.bytes_card = StatCard("Data downloaded")
        self.avg_speed_card = StatCard("Average speed")
        self.peak_speed_card = StatCard("Peak speed")

        cards = QGridLayout()
        cards.setSpacing(12)
        for index, card in enumerate(
            (self.total_card, self.completed_card, self.failed_card,
             self.bytes_card, self.avg_speed_card, self.peak_speed_card)
        ):
            cards.addWidget(card, index // 3, index % 3)
        layout.addLayout(cards)

        charts = QHBoxLayout()
        charts.setSpacing(12)
        self.daily_chart = BarChart("Downloads per day")
        self.bandwidth_chart = BarChart("Bandwidth per day (MB)")
        charts.addWidget(self.daily_chart, 1)
        charts.addWidget(self.bandwidth_chart, 1)
        layout.addLayout(charts)

        self.category_summary = QLabel()
        self.category_summary.setWordWrap(True)
        self.category_summary.setObjectName("card_caption")
        layout.addWidget(self.category_summary)
        layout.addStretch(1)

        self.refresh()

    def refresh(self) -> None:
        dashboard = getattr(getattr(self.context, "analytics", None), "dashboard", None)
        if dashboard is None:
            return
        overview = dashboard.overview()
        self.total_card.set_value(str(overview["total"]))
        self.completed_card.set_value(str(overview["completed"]))
        self.failed_card.set_value(str(overview["failed"]))
        self.bytes_card.set_value(format_bytes(overview["bytes_downloaded"]))
        self.avg_speed_card.set_value(format_speed(overview["avg_speed"]))
        self.peak_speed_card.set_value(format_speed(overview["peak_speed"]))

        daily = dashboard.daily_activity(DAYS)
        self.daily_chart.set_data(
            [(row["date"][5:], row["count"]) for row in daily],
            color="#8B5CF6",
        )
        bandwidth = dashboard.bandwidth_history(DAYS)
        self.bandwidth_chart.set_data(
            [(row["date"][5:], row["bytes"] / (1024 * 1024)) for row in bandwidth],
            color="#22D3EE",
        )

        categories = dashboard.category_breakdown()
        if categories:
            parts = ",  ".join(
                f"{row['name']}: {row['count']} ({format_bytes(row['bytes'])})"
                for row in categories
            )
            self.category_summary.setText("By category:  " + parts)
        else:
            self.category_summary.setText("By category:  no data yet.")
