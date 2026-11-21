from __future__ import annotations

import string
from pathlib import Path

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0B0D14",
        "surface": "#131624",
        "surface_alt": "#1A1E31",
        "border": "#262B44",
        "text": "#E6E9F5",
        "text_muted": "#8A92B5",
        "accent": "#8B5CF6",
        "accent_blue": "#3B82F6",
        "accent_cyan": "#22D3EE",
        "success": "#34D399",
        "warning": "#FBBF24",
        "danger": "#F87171",
    },
    "light": {
        "bg": "#F6F7FB",
        "surface": "#FFFFFF",
        "surface_alt": "#EEF0F7",
        "border": "#D9DEE9",
        "text": "#1B2030",
        "text_muted": "#5A637A",
        "accent": "#7C3AED",
        "accent_blue": "#2563EB",
        "accent_cyan": "#0E7490",
        "success": "#059669",
        "warning": "#B45309",
        "danger": "#DC2626",
    },
}


def build_qss(theme: str = "dark") -> str:
    """Render the QSS template for the given theme, substituting color tokens."""
    colors = THEMES.get(theme, THEMES["dark"])
    path = Path(__file__).parent / f"{theme}.qss"
    if not path.exists():
        path = Path(__file__).parent / "dark.qss"
    template = path.read_text(encoding="utf-8")
    return string.Template(template).substitute(colors)
