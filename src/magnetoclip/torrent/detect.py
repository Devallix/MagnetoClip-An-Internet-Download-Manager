"""Torrent URL detection helpers."""

from __future__ import annotations

import re


def is_magnet_link(url: str) -> bool:
    """Return True if *url* is a magnet: URI."""
    return url.lower().startswith("magnet:?xt=urn:btih:")


def is_torrent_file_url(url: str) -> bool:
    """Return True if *url* points to a .torrent file."""
    return url.lower().rstrip("/").endswith(".torrent")


def is_torrent_url(url: str) -> bool:
    """Return True if *url* is a magnet link or .torrent file URL."""
    if not url or not isinstance(url, str):
        return False
    return is_magnet_link(url) or is_torrent_file_url(url)
