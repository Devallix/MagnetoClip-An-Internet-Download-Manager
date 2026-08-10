"""Media type detection from filenames, URLs, and content types."""

from __future__ import annotations

import re
from pathlib import Path

MEDIA_TYPES = (
    "video",
    "audio",
    "image",
    "document",
    "archive",
    "software",
    "ebook",
    "subtitle",
    "font",
    "unknown",
)

_EXTENSIONS: dict[str, str] = {}
_EXTENSIONS.update(dict.fromkeys(
    ("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts", "mts",
     "m2ts", "3gp", "ogv", "vob", "rmvb", "asf"),
    "video",
))
_EXTENSIONS.update(dict.fromkeys(
    ("mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "opus", "mid", "midi",
     "ape", "aiff", "amr", "ac3"),
    "audio",
))
_EXTENSIONS.update(dict.fromkeys(
    ("jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "tiff", "tif", "ico",
     "heic", "heif", "psd", "raw", "cr2", "nef", "arw"),
    "image",
))
_EXTENSIONS.update(dict.fromkeys(
    ("pdf", "doc", "docx", "txt", "xls", "xlsx", "ppt", "pptx", "odt", "ods",
     "odp", "rtf", "md", "csv", "xml", "html", "htm"),
    "document",
))
_EXTENSIONS.update(dict.fromkeys(
    ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "tgz", "zst", "cab",
     "lz", "lz4"),
    "archive",
))
_EXTENSIONS.update(dict.fromkeys(
    ("exe", "msi", "dmg", "pkg", "appimage", "deb", "rpm", "apk", "ipsw",
     "jar", "bat", "sh", "msix", "appx"),
    "software",
))
_EXTENSIONS.update(dict.fromkeys(
    ("epub", "mobi", "azw", "azw3", "djvu", "fb2"),
    "ebook",
))
_EXTENSIONS.update(dict.fromkeys(
    ("srt", "sub", "vtt", "ass", "ssa"),
    "subtitle",
))
_EXTENSIONS.update(dict.fromkeys(
    ("ttf", "otf", "woff", "woff2", "eot"),
    "font",
))

_CONTENT_TYPE_PREFIXES: dict[str, str] = {
    "video/": "video",
    "audio/": "audio",
    "image/": "image",
    "text/": "document",
    "application/pdf": "document",
    "application/msword": "document",
    "application/vnd.ms-": "document",
    "application/vnd.openxmlformats-": "document",
    "application/zip": "archive",
    "application/x-7z-compressed": "archive",
    "application/x-rar-compressed": "archive",
    "application/x-tar": "archive",
    "application/gzip": "archive",
    "application/x-iso9660-image": "archive",
    "application/octet-stream": "unknown",
}

# URL path segments that indicate streaming media pages (HLS / DASH / adaptive).
_STREAM_HINTS = re.compile(r"\.(m3u8|mpd)(\?|$)", re.IGNORECASE)


def detect_type(
    filename: str | None = None,
    url: str = "",
    content_type: str | None = None,
) -> str:
    """Detect a media type. Content type is the strongest signal, then the
    filename extension, then stream-hint URL patterns."""
    if content_type:
        lowered = content_type.lower().split(";")[0].strip()
        for prefix, media_type in _CONTENT_TYPE_PREFIXES.items():
            if lowered.startswith(prefix):
                if media_type == "unknown":
                    break
                return media_type
    if filename:
        extension = Path(filename).suffix.lower().lstrip(".")
        media_type = _EXTENSIONS.get(extension)
        if media_type is not None:
            return media_type
    if url and _STREAM_HINTS.search(url):
        return "video"
    return "unknown"


def is_streaming_url(url: str) -> bool:
    """True if the URL points at an HLS/DASH adaptive stream manifest."""
    return bool(url and _STREAM_HINTS.search(url))


def category_for_type(media_type: str) -> str:
    """Map a detected media type onto a default category name."""
    mapping = {
        "video": "Videos",
        "audio": "Music",
        "image": "Images",
        "document": "Documents",
        "archive": "Archives",
        "software": "Software",
        "ebook": "Documents",
        "subtitle": "Documents",
        "font": "Documents",
    }
    return mapping.get(media_type, "Other")
