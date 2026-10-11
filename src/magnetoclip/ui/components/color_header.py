"""A table header that paints each column label in its own color."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette, QPen
from PySide6.QtWidgets import QHeaderView


class ColorHeaderView(QHeaderView):
    """Horizontal header with a per-section label color.

    A ``QHeaderView::section { color: ... }`` rule in the app stylesheet wins
    over both ``QTableWidgetItem.setForeground()`` and inline HTML on the
    header label, so the only reliable way to colorize individual columns is
    to paint the text ourselves on top of the style-rendered section.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self._colors: dict[int, QColor] = {}
        self._aligns: dict[int, int] = {}

    def set_section_color(self, section: int, color: QColor | str) -> None:
        """Paint the label of *section* in *color*."""
        self._colors[section] = QColor(color)
        self.viewport().update()

    def set_section_alignment(self, section: int, alignment: int) -> None:
        """Align the label of *section*; defaults to left/centre."""
        self._aligns[section] = int(alignment)
        self.viewport().update()

    def section_color(self, section: int) -> QColor | None:
        return self._colors.get(section)

    def paintSection(self, painter, rect, index) -> None:
        """*index* is the logical section index, as passed by QHeaderView."""
        color = self._colors.get(index)
        model = self.model()
        text = ""
        if color is not None and model is not None and not self.isSortIndicatorShown():
            text = str(model.headerData(index, Qt.Horizontal, Qt.DisplayRole) or "")
        if not text:
            super().paintSection(painter, rect, index)
            return

        super().paintSection(painter, rect, index)

        painter.save()
        # Wipe the muted label the base paint just drew, leaving the section
        # chrome (background plus the 1px separators) intact.
        painter.fillRect(
            rect.adjusted(1, 1, -1, -1), self.palette().brush(QPalette.Base)
        )
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(color))
        alignment = self._aligns.get(index, Qt.AlignLeft | Qt.AlignVCenter)
        painter.drawText(rect.adjusted(8, 0, -8, 0), alignment, text)
        painter.restore()
