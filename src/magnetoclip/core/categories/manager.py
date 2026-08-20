"""Category management and automatic file classification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ...app.context import AppContext
from ...database.models import Category
from ...database.repositories import CategoryRepository
from ...services.logging import get_logger
from ..events.bus import Events

log = get_logger(__name__)

DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {"name": "Documents", "folder": "Documents", "icon": "doc", "color": "#4A90D9"},
    {"name": "Videos", "folder": "Videos", "icon": "video", "color": "#D9534F"},
    {"name": "Music", "folder": "Music", "icon": "music", "color": "#5CB85C"},
    {"name": "Images", "folder": "Pictures", "icon": "image", "color": "#F0AD4E"},
    {"name": "Software", "folder": "Downloads", "icon": "code", "color": "#7E57C2"},
    {"name": "Archives", "folder": "Downloads", "icon": "archive", "color": "#78909C"},
    {"name": "Torrent", "folder": "Torrents", "icon": "download", "color": "#4ADE80"},
    {"name": "Other", "folder": "Downloads", "icon": "file", "color": "#95A5A6"},
]

_EXTENSION_RULES: dict[str, str] = {
    # Documents
    "pdf": "Documents", "doc": "Documents", "docx": "Documents", "txt": "Documents",
    "xls": "Documents", "xlsx": "Documents", "ppt": "Documents", "pptx": "Documents",
    "odt": "Documents", "ods": "Documents", "odp": "Documents", "rtf": "Documents",
    "md": "Documents", "epub": "Documents", "mobi": "Documents", "csv": "Documents",
    # Videos
    "mp4": "Videos", "mkv": "Videos", "avi": "Videos", "mov": "Videos",
    "wmv": "Videos", "flv": "Videos", "webm": "Videos", "m4v": "Videos",
    "ts": "Videos", "mts": "Videos", "m2ts": "Videos", "3gp": "Videos",
    "ogv": "Videos",
    # Music
    "mp3": "Music", "wav": "Music", "flac": "Music", "aac": "Music",
    "ogg": "Music", "wma": "Music", "m4a": "Music", "opus": "Music",
    "mid": "Music", "midi": "Music", "ape": "Music",
    # Images
    "jpg": "Images", "jpeg": "Images", "png": "Images", "gif": "Images",
    "bmp": "Images", "svg": "Images", "webp": "Images", "tiff": "Images",
    "ico": "Images", "heic": "Images", "psd": "Images", "raw": "Images",
    # Software
    "exe": "Software", "msi": "Software", "dmg": "Software", "pkg": "Software",
    "appimage": "Software", "deb": "Software", "rpm": "Software", "apk": "Software",
    "ipsw": "Software", "jar": "Software",
    # Archives
    "zip": "Archives", "rar": "Archives", "7z": "Archives", "tar": "Archives",
    "gz": "Archives", "bz2": "Archives", "xz": "Archives", "iso": "Archives",
    "tgz": "Archives", "zst": "Archives", "cab": "Archives",
}


class CategoryManager:
    """Manages download categories and auto-classification rules."""

    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.session_factory = context.session_factory
        with context.session_factory() as session:
            self._repo = CategoryRepository(session)
            self._ensure_defaults()
        self._by_name: dict[str, Category] = {}
        self.reload()

    def _ensure_defaults(self) -> None:
        for default in DEFAULT_CATEGORIES:
            existing = self._repo.get_by_name(default["name"])
            if existing is None:
                self._repo.add(**default)

    def reload(self) -> None:
        with self.session_factory() as session:
            categories = CategoryRepository(session).list()
        self._by_name = {category.name: category for category in categories}
        self.context.events.post(Events.CATEGORIES_CHANGED, {"categories": [c.name for c in categories]})

    def list(self) -> list[Category]:
        return list(self._by_name.values())

    def get(self, category_id: int) -> Category | None:
        with self.session_factory() as session:
            return CategoryRepository(session).get(category_id)

    def get_by_name(self, name: str) -> Category | None:
        return self._by_name.get(name)

    def add(
        self,
        name: str,
        *,
        folder: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        rules: dict[str, Any] | None = None,
    ) -> Category:
        with self.session_factory() as session:
            repo = CategoryRepository(session)
            if repo.get_by_name(name) is not None:
                raise ValueError(f"category '{name}' already exists")
            category = repo.add(name, folder=folder, icon=icon, color=color, rules=rules)
        self.reload()
        return category

    def remove(self, name: str) -> None:
        with self.session_factory() as session:
            repo = CategoryRepository(session)
            category = repo.get_by_name(name)
            if category is None:
                raise KeyError(name)
            if category.name == "Other":
                raise ValueError("cannot remove the default 'Other' category")
            repo.remove(category)
        self.reload()

    def classify(self, filename: str | None = None, url: str = "") -> Category:
        """Pick a category based on extension and auto-rule extensions."""
        candidate: str | None = None
        if filename:
            extension = Path(filename).suffix.lower().lstrip(".")
            candidate = _EXTENSION_RULES.get(extension)
        if candidate is not None and candidate in self._by_name:
            return self._by_name[candidate]
        # Fall back to per-category custom rules (extension based).
        for category in self._by_name.values():
            rules = category.rules_json or {}
            extensions = [str(e).lower().lstrip(".") for e in rules.get("extensions", [])]
            if filename and Path(filename).suffix.lower().lstrip(".") in extensions:
                return category
        return self._by_name.get("Other") or next(iter(self._by_name.values()))
