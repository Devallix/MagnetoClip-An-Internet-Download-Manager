from __future__ import annotations

from urllib.parse import urlparse

SUPPORTED_SCHEMES = ("http", "https", "ftp")


class InvalidUrlError(ValueError):
    pass


def validate_url(url: str) -> str:
    """Validate a download URL; raises :class:`InvalidUrlError` if unsafe."""
    if not url or not url.strip():
        raise InvalidUrlError("URL is empty")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in SUPPORTED_SCHEMES:
        raise InvalidUrlError(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        raise InvalidUrlError("URL has no host")
    if any(ch in url for ch in ("\n", "\r", "\x00")):
        raise InvalidUrlError("URL contains control characters")
    return url
