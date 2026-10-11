"""Liquid-style animated progress bars.

A progress bar that reads as moving liquid in a tube: a rounded track, a
gradient fill, and a travelling highlight band. One shared ticker drives every
visible bar, so a full table of downloads animates without a timer per cell.

Unlike a stock ``QProgressBar`` the chunk is painted directly, so state and
theme colours are resolved from the theme tokens rather than CSS.
"""

from __future__ import annotations

from math import cos, tau

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProgressBar

from magnetoclip.ui.themes.palette import THEMES

_TICK_MS = 30
# Ticks for one full highlight sweep; larger is slower and calmer.
_SHINE_CYCLE = 90

_STATE_HUES = {
    "downloading": ("accent_cyan", "accent_blue", "accent"),
    "paused": "warning",
    "completed": "success",
    "failed": "danger",
    "": "accent",
}


class _Ticker(QObject):
    """Application-wide animation clock driving every visible liquid bar."""

    tick = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def _advance(self) -> None:
        self._phase += 1
        self.tick.emit(self._phase)


_ticker: _Ticker | None = None


def _shared_ticker() -> _Ticker:
    global _ticker
    if _ticker is None:
        _ticker = _Ticker()
    return _ticker


_GLOBAL_THEME = "dark"
_registry: set[LiquidProgressBar] = set()


def set_theme(name: str) -> None:
    """Switch the global liquid-bar theme and repaint the live bars."""
    global _GLOBAL_THEME
    _GLOBAL_THEME = name
    for bar in list(_registry):
        bar._trigger_repaint()


class LiquidProgressBar(QProgressBar):
    """A gradient, animated ``QProgressBar`` shaped like a tube of liquid."""

    def __init__(self, parent=None, *, theme: str | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._state = "downloading"
        self._phase = 0
        self._indeterminate = False
        self._subscribed = False
        _registry.add(self)
        self.destroyed.connect(lambda *_: _registry.discard(self))

    # ----- public API -----

    def set_state(self, state: str) -> None:
        """Set the fill palette: downloading (animated), paused, completed, failed."""
        if self._state == state:
            return
        self._state = state
        self.update()

    def set_indeterminate(self, value: bool) -> None:
        """Show a sloshing liquid level instead of a value while work is pending."""
        if self._indeterminate == value:
            return
        self._indeterminate = value
        self.update()

    def set_fraction(self, value: float) -> None:
        """Set progress from a 0.0–1.0 fraction."""
        self.setValue(int(max(0.0, min(1.0, value)) * 100))

    def _trigger_repaint(self) -> None:
        self.update()

    # ----- lifecycle / animation subscription -----

    def _set_subscribed(self, on: bool) -> None:
        if self._subscribed == on:
            return
        ticker = _shared_ticker()
        if on:
            ticker.tick.connect(self._on_tick)
        else:
            ticker.tick.disconnect(self._on_tick)
        self._subscribed = on

    def _on_tick(self, phase: int) -> None:
        self._phase = phase
        if self.isVisible():
            self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._set_subscribed(True)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._set_subscribed(False)
        super().hideEvent(event)

    # ----- painting -----

    def _fraction(self) -> float:
        if self._indeterminate:
            # Slosh the liquid level gently (~18–60%) on the shared clock.
            slosh = 0.5 + 0.5 * cos(tau * self._phase / _SHINE_CYCLE)
            return 0.18 + 0.42 * slosh
        span = self.maximum() - self.minimum()
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self.value() - self.minimum()) / span))

    def _animating(self) -> bool:
        if self._indeterminate:
            return True
        if self._state != "downloading":
            return False
        frac = self._fraction()
        return 0.0 < frac < 1.0

    def _theme_colors(self) -> dict[str, str]:
        return THEMES.get(self._theme or _GLOBAL_THEME, THEMES["dark"])

    def _fill_gradient(self, state: str, colors: dict, fill: QRectF) -> QLinearGradient:
        g = QLinearGradient(fill.left(), fill.top(), fill.right(), fill.top())
        hue = _STATE_HUES.get(state) or "accent"
        if state in ("downloading", ""):
            if isinstance(hue, tuple) and len(hue) >= 3:
                g.setColorAt(0.0, QColor(colors["accent_cyan"]))
                g.setColorAt(0.5, QColor(colors["accent_blue"]))
                g.setColorAt(1.0, QColor(colors["accent"]))
                return g
            base = QColor(colors[hue])
            g.setColorAt(0.0, base.lighter(115))
            g.setColorAt(0.55, base)
            g.setColorAt(1.0, base.darker(115))
            return g
        base = QColor(colors[hue])
        g.setColorAt(0.0, base.lighter(115))
        g.setColorAt(0.55, base)
        g.setColorAt(1.0, base.darker(115))
        return g

    def _paint_shine(self, painter: QPainter, fill: QRectF) -> None:
        """A soft white band that sweeps across the fill, like moving liquid."""
        band = max(8.0, fill.width() * 0.22)
        span = fill.width() + band
        offset = (self._phase % _SHINE_CYCLE) / _SHINE_CYCLE * span - band
        band_rect = QRectF(
            fill.left() + offset, fill.top(), band, fill.height()
        ).intersected(fill)
        if not band_rect.isValid():
            return
        glow = QLinearGradient(band_rect.left(), 0, band_rect.right(), 0)
        glow.setColorAt(0.0, QColor(255, 255, 255, 0))
        glow.setColorAt(0.5, QColor(255, 255, 255, 120))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(band_rect, glow)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if rect.width() <= 1.0 or rect.height() <= 1.0:
            painter.end()
            return
        colors = self._theme_colors()
        radius = rect.height() / 2.0

        tube = QPainterPath()
        tube.addRoundedRect(rect, radius, radius)

        painter.save()
        painter.setClipPath(tube)
        painter.fillRect(rect, QColor(colors["surface_alt"]))

        frac = self._fraction()
        if frac > 0.0:
            fill = QRectF(
                rect.left(), rect.top(), rect.width() * frac, rect.height()
            )
            painter.fillRect(fill, self._fill_gradient(self._state, colors, fill))

            gloss = QLinearGradient(0.0, fill.top(), 0.0, fill.bottom())
            gloss.setColorAt(0.0, QColor(255, 255, 255, 60))
            gloss.setColorAt(0.4, QColor(255, 255, 255, 0))
            gloss.setColorAt(1.0, QColor(0, 0, 0, 45))
            painter.fillRect(fill, gloss)

            if self._animating() and self.isVisible():
                self._paint_shine(painter, fill)
        painter.restore()

        painter.setPen(QPen(QColor(colors["border"]), 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()