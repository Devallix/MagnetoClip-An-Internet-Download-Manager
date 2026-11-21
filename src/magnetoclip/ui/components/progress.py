"""Progress bar whose chunk color follows the download state."""

from __future__ import annotations

from PySide6.QtWidgets import QProgressBar


class StyledProgressBar(QProgressBar):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTextVisible(False)
        self.setRange(0, 100)
        self.setValue(0)
        self._state = "idle"

    def set_state(self, state: str) -> None:
        self._state = state
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_fraction(self, value: float) -> None:
        self.setValue(int(max(0.0, min(1.0, value)) * 100))
