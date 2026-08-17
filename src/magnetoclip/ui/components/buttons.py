"""Styled buttons used across the application."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton


class AccentButton(QPushButton):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setProperty("role", "accent")


class DangerButton(QPushButton):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setProperty("role", "danger")


class GhostButton(QPushButton):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setProperty("role", "ghost")


class IconToolButton(QToolButton):
    """An icon-only tool button whose tooltip doubles as its label."""

    def __init__(self, icon=None, text: str = "", parent=None) -> None:
        super().__init__(parent)
        if icon is not None:
            self.setIcon(icon)
        self.setText(text)
        self.setToolTip(text)
        self.setIconSize(QSize(18, 18))
        self.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.setCursor(Qt.PointingHandCursor)


class VerticalIconButton(QToolButton):
    """A tool button that stacks a colorful text label below its icon."""

    def __init__(self, icon=None, text: str = "", parent=None) -> None:
        super().__init__(parent)
        if icon is not None:
            self.setIcon(icon)
        self.setText(text)
        self.setToolTip(text)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setIconSize(QSize(22, 22))
        self.setCursor(Qt.PointingHandCursor)


class CategoryButton(QFrame):
    """Clickable sidebar entry: icon, label, and a right-aligned counter."""

    clicked = Signal(str)

    def __init__(self, key: str, text: str, icon=None, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self.setObjectName("category_button")
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(8)

        self.icon_label = QLabel()
        if icon is not None:
            self.icon_label.setPixmap(icon.pixmap(QSize(18, 18)))
        layout.addWidget(self.icon_label)

        self.name_label = QLabel(text)
        self.name_label.setObjectName("category_name")
        layout.addWidget(self.name_label, 1)

        self.count_label = QLabel("0")
        self.count_label.setObjectName("category_count")
        self.count_label.setAlignment(Qt.AlignCenter)
        self.count_label.setMinimumWidth(24)
        layout.addWidget(self.count_label)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(event)

    def set_count(self, count: int) -> None:
        self.count_label.setText(str(count))

    def set_compact(self, compact: bool) -> None:
        """Hide the label and counter for a collapsed (icon-only) sidebar."""
        self.name_label.setVisible(not compact)
        self.count_label.setVisible(not compact)
        self.layout().setContentsMargins(8 if compact else 10, 8, 8, 8)
        self.icon_label.setAlignment(Qt.AlignCenter if compact else Qt.AlignLeft | Qt.AlignVCenter)
        self.setToolTip(self.name_label.text() if compact else "")

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)
