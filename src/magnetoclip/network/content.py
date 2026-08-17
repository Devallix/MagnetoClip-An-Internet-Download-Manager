"""Heuristics for deciding whether a fetched body is really a file.

Broken links and login walls frequently answer a file download request with an
HTML error page (200/404) instead of the requested bytes. Saving that HTML over
a .mp4/.zip filename produces a tiny, unopenable file that looks "completed",
so the engine refuses HTML content unless the target is meant to be a page.
"""

from __future__ import annotations

from pathlib import PurePosixPath

HTML_EXTENSIONS = frozenset({".html", ".htm", ".xhtml", ".mht", ".mhtml"})


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
