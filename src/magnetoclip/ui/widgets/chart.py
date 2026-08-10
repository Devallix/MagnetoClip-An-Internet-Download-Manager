"""Lightweight QPainter charts for the analytics dashboard."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from ..themes.palette import THEMES

THEME_COLORS = THEMES["dark"]


class BarChart(QFrame):
    """Simple vertical bar chart with value labels and muted baseline."""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._data: list[tuple[str, float]] = []
        self._bar_color = QColor(THEME_COLORS["accent"])
        self._muted = QColor(THEME_COLORS["text_muted"])
        self._line = QColor(THEME_COLORS["border"])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("page_subtitle")
        layout.addWidget(title_label)

        self._canvas = QWidget()
        self._canvas.setMinimumHeight(150)
        self._canvas.paintEvent = self._paint  # type: ignore[method-assign]
        layout.addWidget(self._canvas, 1)

    def set_data(self, data: Sequence[tuple[str, float]], color: str | None = None) -> None:
        self._data = [(str(label), float(value)) for label, value in data]
        if color:
            self._bar_color = QColor(color)
        self._canvas.update()

    def clear(self) -> None:
        self._data = []
        self._canvas.update()

    def _paint(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self._canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self._canvas.rect()
        if not self._data or rect.width() < 20 or rect.height() < 20:
            painter.setPen(self._muted)
            painter.drawText(rect, Qt.AlignCenter, "No data yet.")
            painter.end()
            return

        painter.fillRect(rect, QColor(THEME_COLORS["surface"]))
        pad_left, pad_bottom = 34, 20
        plot = rect.adjusted(pad_left, 8, -8, -pad_bottom)
        max_value = max(v for _, v in self._data) or 1.0
        n = len(self._data)
        slot = plot.width() / n
        bar_width = max(4.0, slot * 0.6)

        painter.setPen(self._line)
        painter.drawLine(plot.left(), plot.bottom(), plot.right(), plot.bottom())

        for i, (label, value) in enumerate(self._data):
            height = plot.height() * (value / max_value)
            x = plot.left() + slot * i + (slot - bar_width) / 2
            y = plot.bottom() - height
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._bar_color)
            painter.drawRoundedRect(int(x), int(y), int(bar_width), max(1, int(height)), 3, 3)

            painter.setPen(self._muted)
            font = painter.font()
            font.setPixelSize(10)
            painter.setFont(font)
            painter.drawText(int(x - slot / 2), plot.bottom() + 14, int(slot), 12, Qt.AlignCenter, label)

        painter.end()
