"""Cheap, network-free classification of YouTube URLs.

``magnetoclip/media/playlist.py`` already ships ``is_playlist_url`` /
``extract_playlist_video_urls`` (header-only expansion). This module keeps that
conservative spirit and formalises *what kind* of YouTube URL a user pasted so
the UI can decide between the single-video flow and the playlist preview flow.

Only ``youtube.com`` (incl. ``music.``, ``m.``) and ``youtu.be`` hosts are
recognised, so no other domain ever changes behaviour.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from .types import YouTubeURLType

#: Playlist ids that yt-dlp recognises as playlist-like (`PL…` playlists,
#: `RD…` mixes, `OLAK…` album auto-mixes, `UU…` user playlists, `FL…` favourites,
#: `WL…` watch later). Kept identical in spirit to the legacy
#: ``magnetoclip.media.playlist.is_playlist_url`` prefix check.
_PLAYLIST_PREFIXES = ("PL", "RD", "OLAK", "UU", "FL", "WL")

#: YouTube hosts we treat as first-party.
_YOUTUBE_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
)


def host_of(url: str) -> str | None:
    """Return the lowercase host of *url* (without port/userinfo), or None for
    malformed / non-http input."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    netloc = (parsed.netloc or "").lower()
    if not netloc:
        return None
    if ":" in netloc:
        netloc = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    return netloc.rsplit("@", 1)[-1]


def is_youtube(url: str) -> bool:
    """True when *url* is one of the first-party YouTube hosts."""
    host = host_of(url)
    if not host:
        return False
    return host == "youtu.be" or (host == "youtube.com" or host.endswith(".youtube.com"))


def _parts(url: str) -> tuple[str, str, dict[str, list[str]]]:
    """Return (host, path, query) with a tolerant fallback for garbage input."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "", "", {}
    host = (parsed.netloc or "").lower()
    if ":" in host:
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    host = host.rsplit("@", 1)[-1]
    try:
        query = parse_qs(parsed.query, keep_blank_values=False)
    except ValueError:
        query = {}
    return host, parsed.path, query


def _is_playlist_id(value: str | None) -> bool:
    return bool(value) and value.startswith(_PLAYLIST_PREFIXES)


class YouTubeURLDetector:
    """Recognises the shape of a YouTube URL without touching the network."""

    @classmethod
    def classify(cls, url: str) -> YouTubeURLType:
        if not url or not isinstance(url, str):
            return YouTubeURLType.unknown
        host, path, query = _parts(url)
        if not is_youtube(url):
            return YouTubeURLType.unknown

        list_id = (query.get("list") or [None])[0]
        lower_path = path.lower().rstrip("/")

        if host == "youtu.be":
            video = path.strip("/").split("/", 1)[0] if path.strip("/") else ""
            if not video:
                return YouTubeURLType.unknown
            if list_id:
                return YouTubeURLType.video_with_playlist
            return YouTubeURLType.video

        if lower_path.endswith("/playlist"):
            return (
                YouTubeURLType.playlist
                if _is_playlist_id(list_id)
                else YouTubeURLType.unknown
            )

        video_id = (query.get("v") or [None])[0]
        for prefix in ("/shorts/", "/embed/", "/live/"):
            if lower_path.startswith(prefix):
                video_id = video_id or path[len(prefix):].split("/", 1)[0]
                break

        if not video_id:
            return YouTubeURLType.unknown
        if list_id:
            return YouTubeURLType.video_with_playlist
        return YouTubeURLType.video


def classify(url: str) -> YouTubeURLType:
    """Module-level helper; see :meth:`YouTubeURLDetector.classify`."""
    return YouTubeURLDetector.classify(url)


__all__ = ["YouTubeURLDetector", "classify", "host_of", "is_youtube"]