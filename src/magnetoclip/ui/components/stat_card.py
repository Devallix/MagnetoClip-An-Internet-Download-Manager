"""Dashboard stat card."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


class StatCard(QFrame):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("stat_card")
        self.setFixedHeight(96)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)

        self.value_label = QLabel("--")
        self.value_label.setObjectName("stat_value")
        self.title_label = QLabel(title)
        self.title_label.setObjectName("stat_label")
        layout.addWidget(self.value_label)
        layout.addWidget(self.title_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)
