"""Sidebar download categories and their mapping to detected file types."""

from __future__ import annotations

from typing import Any

CATEGORY_ORDER = [
    "image",
    "video",
    "audio",
    "document",
    "archive",
    "software",
    "torrent",
    "other",
]

CATEGORY_LABELS = {
    "image": "Image",
    "video": "Video",
    "audio": "Audio",
    "document": "Document",
    "archive": "Compressed",
    "software": "App / Software",
    "torrent": "Torrent",
    "other": "Other",
}

_TYPES: dict[str, frozenset[str]] = {
    "image": frozenset({"image"}),
    "video": frozenset({"video"}),
    "audio": frozenset({"audio"}),
    "document": frozenset({"document", "ebook", "subtitle", "font"}),
    "archive": frozenset({"archive"}),
    "software": frozenset({"software"}),
    "torrent": frozenset({"torrent"}),
    "other": frozenset({"unknown"}),
}


def snapshot_category(snapshot: dict[str, Any]) -> str:
    """Return the sidebar category a download snapshot belongs to."""
    detected = snapshot.get("detected_type")
    for category, types in _TYPES.items():
        if detected in types:
            return category
    return "other"


def snapshot_in_category(snapshot: dict[str, Any], category: str | None) -> bool:
    if category in (None, "", "all"):
        return True
    return snapshot_category(snapshot) == category
