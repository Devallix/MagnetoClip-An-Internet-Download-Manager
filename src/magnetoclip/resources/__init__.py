"""Runtime-safe paths for bundled resources (works from src layout and PyInstaller)."""

from __future__ import annotations

import sys
from pathlib import Path

_RESOURCES = Path(__file__).resolve().parent


def resources_dir() -> Path:
    """Return the on-disk resources directory regardless of frozen/unfrozen mode."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
        internal = base / "_internal"
        candidates = [internal / "magnetoclip" / "resources", base / "magnetoclip" / "resources"]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
    return _RESOURCES


def resource_path(*parts: str) -> Path:
    """Absolute path to a bundled resource, e.g. resource_path("icons", "logo.png")."""
    return resources_dir().joinpath(*parts)


def app_icon() -> object:
    """QIcon built from the bundled logo (multi-size aware)."""
    from PySide6.QtGui import QIcon

    path = resource_path("icons", "logo.ico")
    icon = QIcon(str(path))
    if icon.isNull():
        png = resource_path("icons", "logo.png")
        icon = QIcon(str(png))
    return icon
