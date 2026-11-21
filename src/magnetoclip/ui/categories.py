"""Sidebar download categories and their mapping to detected file types."""

from __future__ import annotations

from typing import Any

# The six detection categories the browser extension queues resources into. They
# are the vocabulary of the Detected page filters, the ``magneto://open-detected``
# URI and the extension popup, and they must stay identical on both sides.
DETECTION_CATEGORIES = [
    "images",
    "videos",
    "audio",
    "compressed",
    "documents",
    "other",
]

DETECTION_CATEGORY_LABELS = {
    "images": "Images",
    "videos": "Videos",
    "audio": "Audio",
    "compressed": "Compressed",
    "documents": "Documents",
    "other": "Other",
}

# Detection category -> the sidebar/detected-type vocabulary used for icons and
# the Type column.
DETECTION_CATEGORY_TO_TYPE = {
    "images": "image",
    "videos": "video",
    "audio": "audio",
    "compressed": "archive",
    "documents": "document",
    "other": "other",
}

_DETECTION_TYPE_TO_CATEGORY = {
    "image": "images",
    "video": "videos",
    "audio": "audio",
    "archive": "compressed",
    "compressed": "compressed",
    "document": "documents",
    "ebook": "documents",
    "subtitle": "documents",
    "software": "other",
    "torrent": "other",
    "webpage": "other",
    "stream": "videos",
    "unknown": "other",
    "other": "other",
}


def normalize_detection_category(value: Any) -> str:
    """Normalize a category filter; ``""`` means "all"."""
    raw = str(value or "").strip().lower()
    if not raw or raw == "all":
        return ""
    if raw in DETECTION_CATEGORIES:
        return raw
    if f"{raw}s" in DETECTION_CATEGORIES:
        return f"{raw}s"
    return _DETECTION_TYPE_TO_CATEGORY.get(raw, "")


def detection_category(detected_type: Any, category: Any = None) -> str:
    """Category of a detection row, honouring the category the extension sent.

    The extension classifies centrally and sends ``category`` alongside the
    app-level ``detected_type``; the type is only used as a fallback so rows
    written by older builds still land somewhere sensible.
    """
    explicit = normalize_detection_category(category)
    if explicit:
        return explicit
    return _DETECTION_TYPE_TO_CATEGORY.get(str(detected_type or "").lower(), "other")


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


def detected_type_category(detected_type: Any) -> str:
    """Category for a raw ``detected_type`` value (``None`` falls to other)."""
    for category, types in _TYPES.items():
        if detected_type in types:
            return category
    return "other"


def snapshot_category(snapshot: dict[str, Any]) -> str:
    """Return the sidebar category a download snapshot belongs to."""
    return detected_type_category(snapshot.get("detected_type"))


def snapshot_in_category(snapshot: dict[str, Any], category: str | None) -> bool:
    if category in (None, "", "all"):
        return True
    return snapshot_category(snapshot) == category
