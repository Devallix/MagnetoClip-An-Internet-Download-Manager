"""MagnetoClip theme system: color tokens, QSS templates, and loading."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMainWindow

from .palette import THEMES, build_qss


def _palette_for(theme: str) -> QPalette:
    """A palette built from the theme tokens so unstyled widgets stay dark."""
    colors = THEMES.get(theme, THEMES["dark"])
    color = lambda name: QColor(colors[name])  # noqa: E731

    palette = QPalette()
    palette.setColor(QPalette.Window, color("bg"))
    palette.setColor(QPalette.WindowText, color("text"))
    palette.setColor(QPalette.Base, color("surface"))
    palette.setColor(QPalette.AlternateBase, color("surface_alt"))
    palette.setColor(QPalette.Text, color("text"))
    palette.setColor(QPalette.Button, color("surface"))
    palette.setColor(QPalette.ButtonText, color("text"))
    palette.setColor(QPalette.Highlight, color("accent"))
    palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ToolTipBase, color("surface_alt"))
    palette.setColor(QPalette.ToolTipText, color("text"))
    palette.setColor(QPalette.PlaceholderText, color("text_muted"))
    palette.setColor(QPalette.Link, color("accent"))
    palette.setColor(QPalette.LinkVisited, color("accent_cyan"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, color("text_muted"))
    palette.setColor(QPalette.Disabled, QPalette.Button, color("surface"))
    palette.setColor(QPalette.Disabled, QPalette.Base, color("surface"))
    return palette


def apply_theme(window: QMainWindow, *, theme: str = "dark") -> None:
    """Apply a QSS theme and matching palette to the whole application."""
    app = QApplication.instance()
    if app is None:
        return
    app.setStyleSheet(build_qss(theme))
    app.setPalette(_palette_for(theme))
    from magnetoclip.ui.components.icons import set_theme as _set_icon_theme

    _set_icon_theme(theme)
    window.update()


__all__ = ["apply_theme", "build_qss", "THEMES"]
