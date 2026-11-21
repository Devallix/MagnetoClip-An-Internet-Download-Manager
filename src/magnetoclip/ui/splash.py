"""Splash screen shown during application startup."""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Property,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from magnetoclip.resources import resource_path
from magnetoclip.version import __app_name__, __tagline__, __version__

# Matches the accent used across the app's themes so the splash does not look
# like it came from a different product.
_ACCENT = QColor(56, 152, 255)
_ACCENT_SOFT = QColor(120, 195, 255)
_SURFACE = QColor(25, 32, 42)


class SplashScreen(QWidget):
    """Custom splash screen with logo, version, spinner, and credit."""

    WIDTH = 400
    HEIGHT = 350

    # Smooth 60fps motion; a 30ms timer with 10-degree steps reads as a stutter.
    FRAME_MS = 16
    SWEEP_DEG = 6.0
    TRAIL_SPAN_DEG = 270.0

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MagnetoClip")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.WIDTH, self.HEIGHT)

        self._angle = 0.0
        self._opacity = 1.0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._advance)
        self._spinner_timer.start(self.FRAME_MS)

        self._icon = self._load_icon()

    def _load_icon(self) -> QPixmap:
        img_path = resource_path("icons", "logo.png")
        return QPixmap(str(img_path))

    def _advance(self) -> None:
        self._angle = (self._angle + self.SWEEP_DEG) % 360.0
        self.update()

    # --- opacity animation, so the dismissal is a cross-fade, not a hard cut ---

    def get_opacity(self) -> float:
        return self._opacity

    def set_opacity(self, value: float) -> None:
        self._opacity = value
        self.update()

    opacity = Property(float, get_opacity, set_opacity)

    def _draw_spinner(self, painter: QPainter, cx: int, cy: int) -> None:
        """Draw an indeterminate progress ring.

        A tapered arc sweeping over a faint track: the arc fades to nothing
        along its tail, which is the standard "working, but not stuck" cue and
        reads as deliberate motion rather than a blinking dot loop.
        """
        outer_r = 21.0
        box = QRectF(cx - outer_r, cy - outer_r, outer_r * 2, outer_r * 2)

        # Track: a barely-there full ring so the arc has something to travel on.
        painter.setPen(QPen(QColor(255, 255, 255, 26), 3.0, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(box, 0, 360 * 16)

        # Indicator: a 270 degree arc drawn as short segments whose alpha ramps
        # from fully transparent at the tail to solid at the leading edge. Done
        # per segment rather than with a conic gradient so the fade cannot come
        # out mirrored depending on the painter's coordinate direction.
        steps = 34
        span = self.TRAIL_SPAN_DEG
        painter.setBrush(Qt.NoBrush)
        for i in range(steps):
            frac = i / (steps - 1)
            angle = math.radians(self._angle - span * frac)
            x = cx + outer_r * math.cos(angle)
            y = cy + outer_r * math.sin(angle)
            # Head is solid, tail transparent; eased so the fade is not linear.
            alpha = int(255 * ((1.0 - frac) ** 0.6))
            painter.setPen(
                QPen(
                    QColor(_ACCENT_SOFT.red(), _ACCENT_SOFT.green(), _ACCENT_SOFT.blue(), alpha),
                    3.4,
                    Qt.SolidLine,
                    Qt.RoundCap,
                )
            )
            painter.drawPoint(QPointF(x, y))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setBrush(_SURFACE)
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
        y_offset += 38

        self._draw_spinner(painter, self.WIDTH // 2, y_offset + 8)

        y_offset += 42

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
        """Stop animating and cross-fade out."""
        self._spinner_timer.stop()
        if not self.isVisible():
            self.close()
            return
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(180)
        animation.setStartValue(1.0)
        animation.setEndValue(0.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(self.close)
        animation.start(QPropertyAnimation.DeleteWhenStopped)
        self._fade_animation = animation

