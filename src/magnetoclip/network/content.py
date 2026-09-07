"""Heuristics for deciding whether a fetched body is really a file.

Broken links and login walls frequently answer a file download request with an
HTML error page (200/404) instead of the requested bytes. Saving that HTML over
a .mp4/.zip filename produces a tiny, unopenable file that looks "completed",
so the engine refuses HTML content unless the target is meant to be a page.
"""

from __future__ import annotations

from pathlib import PurePosixPath

HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml", ".mht", ".mhtml"})

# A small map from common Content-Type media types to a filename extension.
# Used to give extensionless CDN URLs (e.g. Google's encrypted-tbn0.gstatic.com
# image thumbnails) a usable extension based on what the server actually serves.
_CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "image/avif": "avif",
    "image/x-icon": "ico",
    "application/pdf": "pdf",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/x-rar-compressed": "rar",
}


def content_type_extension(content_type: str | None) -> str | None:
    """Return a filename extension (no dot) for a Content-Type, or None."""
    if not content_type:
        return None
    media = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXTENSIONS.get(media)


def looks_like_html_content_type(content_type: str | None) -> bool:
    """True when a Content-Type header names an HTML family document."""
    if not content_type:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return "html" in media_type


def is_html_filename(filename: str | None) -> bool:
    """True when the target filename is explicitly meant to be a web page."""
    if not filename:
        return False
    return PurePosixPath(filename).suffix.lower() in HTML_EXTENSIONS


def should_reject_html_body(
    content_type: str | None,
    filename: str | None,
) -> bool:
    """True when an HTML response is a corrupt substitute for a real file.

    HTML is accepted only when the target filename is a page name (.html, ...).
    Anything else is the signature of an error page being served instead of
    the file.
    """
    if not looks_like_html_content_type(content_type):
        return False
    return not is_html_filename(filename)
