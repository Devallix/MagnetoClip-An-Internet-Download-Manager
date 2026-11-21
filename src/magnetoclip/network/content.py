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


def sniff_extension(data: bytes | bytearray | memoryview | None) -> str | None:
    """Return an extension (no dot) for common media magic bytes, or None.

    Used when in-memory bytes (Telegram ``blob:`` media) arrive without a usable
    filename or MIME type: the Blob API often reports an empty Content-Type for
    untyped blobs, so the real format is recovered from the leading bytes.
    """
    if not data:
        return None
    buf = bytes(data)
    if len(buf) < 12:
        return None

    if buf[0] == 0xFF and buf[1] == 0xD8 and buf[2] == 0xFF:
        return "jpg"
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if buf[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "webp"
    if buf[:2] == b"BM":
        return "bmp"
    if buf[:2] == b"\x00\x00" and buf[4:8] == b"ftyp":
        brand = buf[8:12].lower()
        if brand[:3] in (b"avi", b"f4v") or brand[:3] == b"mif" or brand in (
            b"heic", b"heif", b"heix", b"hevc", b"hevx", b"mif1", b"msf1",
        ):
            return "heic"
        return "mp4"
    if buf[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if buf[:4] == b"OggS":
        return "ogg"
    if buf[:3] == b"ID3" or (buf[0] == 0xFF and (buf[1] & 0xE0) == 0xE0):
        return "mp3"
    if buf[:4] == b"RIFF" and buf[8:12] == b"WAVE":
        return "wav"
    if buf[:4] == b"fLaC":
        return "flac"
    if buf[:4] == b"%PDF":
        return "pdf"
    return None


def blob_extension(mime_type: str | None, data: bytes | None) -> str | None:
    """Resolve an extension for in-memory blob bytes.

    Prefers the MIME type the Blob reported, then falls back to magic-byte
    sniffing (Telegram creates untyped blobs that report an empty Content-Type).
    """
    ext = content_type_extension(mime_type)
    if ext:
        return ext
    return sniff_extension(data)


def ensure_extension(
    filename: str | None,
    mime_type: str | None = None,
    data: bytes | None = None,
) -> str | None:
    """Return *filename* with an extension appended when it has none.

    A bare name (e.g. a user-typed ``yamal`` or a Telegram extensionless media
    URL) yields a file Windows cannot open, so the format is recovered from the
    MIME type or magic bytes and appended. Names that already carry an extension
    are returned unchanged.
    """
    if not filename:
        return filename
    if "." in PurePosixPath(filename).name:
        return filename
    ext = blob_extension(mime_type, data)
    if ext:
        return f"{filename}.{ext}"
    return filename


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
