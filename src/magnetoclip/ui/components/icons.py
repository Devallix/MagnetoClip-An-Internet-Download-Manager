"""UI icons loaded from bundled icons8.com assets (fluency style).

Icons are packaged as PNG files under ``resources/icons`` and loaded
through the resource resolver so they work from both the source tree and
a frozen (PyInstaller) build. The fluency set is color-fixed, so theme
switching only clears the icon cache.
"""

from __future__ import annotations

from PySide6.QtGui import QIcon

from magnetoclip.resources import resource_path

_current_theme = "dark"
_cache: dict[tuple[str, str], QIcon] = {}

# icon key -> icons8 asset file name (without extension)
_NAV_FILES = {
    "overview": "home",
    "downloads": "download",
    "detected": "tag",
    "queue": "list",
    "completed": "checkmark",
    "scheduler": "clock",
    "analytics": "statistics",
    "browser": "internet",
    "settings": "settings",
    "about": "info",
    "all": "layers",
    "menu": "menu",
}

_TOOL_FILES = {
    "add": "add",
    "start": "play",
    "pause": "pause",
    "remove": "trash",
    "select_all": "checkmark",
}

_TYPE_FILES = {
    "image": "image",
    "video": "video",
    "audio": "musical-notes",
    "document": "document",
    "ebook": "document",
    "subtitle": "document",
    "font": "document",
    "archive": "cardboard-box",
    "software": "monitor",
    "other": "question-mark",
}

_TYPE_COLORS = {
    "image": "#A78BFA",
    "video": "#F87171",
    "audio": "#22D3EE",
    "document": "#60A5FA",
    "archive": "#FBBF24",
    "software": "#34D399",
    "other": "#94A3B8",
}


def set_theme(name: str) -> None:
    """Switch the active theme (kept for API compatibility)."""
    global _current_theme, _cache
    _current_theme = name
    _cache = {}


def _load(kind: str, file_key: str) -> QIcon:
    key = (kind, file_key)
    icon = _cache.get(key)
    if icon is not None:
        return icon
    path = resource_path("icons", f"{file_key}.png")
    icon = QIcon(str(path))
    _cache[key] = icon
    return icon


def _type_key(media_type: str | None) -> str:
    return _TYPE_FILES.get(media_type or "", "other")


def nav_icon(name: str) -> QIcon:
    """Icon for a navigation / action entry."""
    file_key = _NAV_FILES.get(name)
    if file_key is None:
        raise KeyError(f"unknown nav icon: {name!r}")
    return _load("nav", file_key)


def category_icon(name: str) -> QIcon:
    """Icon for a sidebar category."""
    return _load("category", _type_key(name))


def tool_icon(name: str) -> QIcon:
    """Icon for a page tool button (add, start, pause, remove)."""
    file_key = _TOOL_FILES.get(name)
    if file_key is None:
        raise KeyError(f"unknown tool icon: {name!r}")
    return _load("tool", file_key)


def type_icon(media_type: str | None) -> QIcon:
    """Icon representing a detected file type."""
    return _load("type", _type_key(media_type))


def type_color(media_type: str | None) -> str:
    return _TYPE_COLORS[_type_key(media_type)]
