"""Splash screen shown during application startup."""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from magnetoclip.version import __app_name__, __tagline__, __version__


class SplashScreen(QWidget):
    """Custom splash screen with logo, version, spinner, and credit."""

    WIDTH = 400
    HEIGHT = 350

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MagnetoClip")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.WIDTH, self.HEIGHT)

        self._angle = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._rotate)
        self._spinner_timer.start(30)

        self._icon = self._load_icon()

    def _load_icon(self) -> QPixmap:
        img_path = Path(__file__).resolve().parents[3] / "img" / "logo.png"
        return QPixmap(str(img_path))

    def _rotate(self) -> None:
        self._angle = (self._angle + 10) % 360
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setBrush(QColor(25, 32, 42))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, self.WIDTH, self.HEIGHT, 16, 16)

        y_offset = 30

        if not self._icon.isNull():
            icon = self._icon.scaled(
                64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            x = (self.WIDTH - icon.width()) // 2
            painter.drawPixmap(x, y_offset, icon)
            y_offset += 74

        painter.setPen(QColor(255, 255, 255))
        font = painter.font()
        font.setPointSize(18)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(0, y_offset, self.WIDTH, 30, Qt.AlignHCenter, __app_name__)
        y_offset += 35

        painter.setPen(QColor(180, 180, 180))
        font.setPointSize(10)
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(0, y_offset, self.WIDTH, 20, Qt.AlignHCenter, __tagline__)
        y_offset += 30

        painter.setPen(QColor(255, 255, 255))
        font.setPointSize(11)
        painter.setFont(font)
        version_text = f"v.{__version__}"
        painter.drawText(0, y_offset, self.WIDTH, 25, Qt.AlignHCenter, version_text)
        y_offset += 35

        cx, cy = self.WIDTH // 2, y_offset + 18
        outer_r, inner_r = 18, 12
        painter.setPen(QColor(100, 100, 100))
        painter.drawEllipse(cx - outer_r, cy - outer_r, outer_r * 2, outer_r * 2)

        painter.setPen(QColor(0, 150, 255))
        for i in range(8):
            alpha = int(255 * (1 - i / 8))
            painter.setPen(QColor(0, 150, 255, alpha))

            rad = math.radians(self._angle - i * 45)
            x = cx + int(inner_r * math.cos(rad))
            y = cy + int(inner_r * math.sin(rad))
            painter.drawEllipse(x - 2, y - 2, 5, 5)

        y_offset += 50

        painter.setPen(QColor(150, 150, 150))
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(
            30, y_offset, self.WIDTH - 60, 40,
            Qt.AlignHCenter | Qt.AlignTop,
            "Advanced Download Manager — Capture the Web"
        )
        y_offset += 40

        painter.setPen(QColor(120, 120, 120))
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(0, y_offset, self.WIDTH, 20, Qt.AlignHCenter, "powered by Devallix")

        painter.end()

    def close_with_fade(self) -> None:
        """Close the splash screen."""
        self._spinner_timer.stop()
        self.close()
