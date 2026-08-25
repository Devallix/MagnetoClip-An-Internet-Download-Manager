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

        self._accent_color: str | None = None

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)

    def set_accent(self, color: str | None) -> None:
        """Set a colored left-border accent on this card.

        Pass a hex color string (e.g. ``"#3B82F6"``) to enable the accent,
        or ``None`` to remove it.  Only works when the card is inside a
        layout that gives it a fixed or minimum width.
        """
        if color == self._accent_color:
            return
        self._accent_color = color
        if color:
            self.setStyleSheet(
                f"QFrame#stat_card {{ border-left: 3px solid {color}; }}"
            )
        else:
            self.setStyleSheet("")
