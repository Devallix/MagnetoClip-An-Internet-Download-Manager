from __future__ import annotations

import re
from urllib.parse import unquote

_FILENAME_STAR = re.compile(r"filename\*=([^']*)''(.+)", re.IGNORECASE)
_FILENAME = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)


def parse_content_disposition(value: str | None) -> str | None:
    """Extract a filename from a ``Content-Disposition`` header value."""
    if not value:
        return None
    match = _FILENAME_STAR.search(value)
    if match:
        return unquote(match.group(2))
    match = _FILENAME.search(value)
    if match:
        return unquote(match.group(1).strip())
    return None
