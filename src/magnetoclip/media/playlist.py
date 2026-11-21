"""YouTube playlist expansion (``media/playlist.py``).

Pasting a **playlist URL** — ``youtube.com/playlist?list=…`` or a
``/watch?v=…&list=…`` watch URL that carries a ``list`` parameter (e.g. a
mix or a "watch later" entry) — resolves to **several videos**. Today that URL
goes to :func:`magnetoclip.media.streaming.resolve_stream` with
``noplaylist: True``, so a bare playlist either resolves to a single entry or
raises ``StreamResolutionError("playlist contains no entries")``.

This module gives the app a *detect → expand* capability that fits the
existing engine instead of fighting it:

1. :func:`is_playlist_url` — recognise a playlist URL cheaply (no network).
2. :func:`extract_playlist_video_urls` — enumerate the *videos* with yt-dlp
   ``flat_playlist`` extraction (header-only; **never downloads**), returning
   one absolute ``https://www.youtube.com/watch?v=…`` per entry in order.
3. The caller (the download manager) creates **one :class:`Download` row per
   video** and queues all of them, so every entry flows through the existing
   per-download pipeline — quality selection, yt-dlp resolve, HLS engine
   support, progress/ETA, pause/resume, FFmpeg merge — exactly as if each had
   been pasted on its own.

Transport matches :mod:`magnetoclip.media.streaming`: same cookiefile /
``Cookie`` header, same ``Referer``-style headers, same optional
impersonation. If enumeration yields nothing useful (empty/expired playlist,
fully region-locked, or a ``blob:``-only player the flat fetch cannot see),
:func:`extract_playlist_video_urls` returns ``[]`` so the manager can fall
through to its existing single-URL / browser paths unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from yt_dlp.extractor.youtube import YoutubeIE

log = logging.getLogger(__name__)

# YouTube playlist URLs we treat specially. Kept deliberately conservative:
# only ``youtube.com`` (incl. ``music.youtube.com`` / ``youtu.be``) so a
# non-YouTube URL never changes behaviour.
_YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")


def _host_of(url: str) -> str | None:
    try:
        netloc = (urlparse(url).netloc or "").lower()
    except ValueError:
        return None
    return netloc.rsplit("@", 1)[-1].split(":", 1)[0] if ":" in netloc else netloc


def _is_youtube(url: str) -> bool:
    host = _host_of(url)
    if not host:
        return False
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"


def is_playlist_url(url: str) -> bool:
    """True if *url* is a YouTube URL whose *list* parameter identifies a
    playlist/mix (or a watch URL that explicitly belongs to one).

    Cheap, header-free: classifies the URL without any network access.
    ``&list=`` on any YouTube URL (playlist, watch, music, shorts that carry a
    list) counts; ``youtu.be`` short links count only when they also carry
    ``?list=``.
    """
    from magnetoclip.media.youtube.detector import classify
    from magnetoclip.media.youtube.types import YouTubeURLType

    kind = classify(url)
    return kind in (YouTubeURLType.playlist, YouTubeURLType.video_with_playlist)


def _watch_url(video_id: str) -> str:
    """A canonical, referable watch URL for *video_id* (https)."""
    return "https://www.youtube.com/watch?v=" + video_id


def extract_playlist_video_urls(
    playlist_url: str,
    *,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    limit: int | None = None,
) -> list[str]:
    """Enumerate the videos in *playlist_url* without downloading anything.

    Returns a list of absolute ``https://www.youtube.com/watch?v=…`` URLs in
    playlist order. ``[]`` when the playlist is empty / unreadable, the URL is
    not a YouTube playlist, or enumeration finds no entries.

    *cookies* are handed to yt-dlp as a cookiefile when provided (same cookie
    handling as the streaming engine). *headers* (e.g. an extension-captured
    ``Referer``) are forwarded as extra headers. *limit* caps the number of
    entries returned (useful for very long mixes); ``None`` returns them all.

    This reuses the *same* yt-dlp transport the app already has, so
    Cloudflare/bot-walls and region locks behave identically to the rest of
    the downloader — no second extraction stack to keep in sync.
    """
    if not is_playlist_url(playlist_url):
        return []

    from yt_dlp import YoutubeDL

    def _cookie_file_path() -> Path | None:
        if not cookies:
            return None
        import tempfile

        fd, name = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# Netscape HTTP Cookie File\n")
            for name_, value in (cookies or {}).items():
                fh.write(f"youtube.com\tTRUE\t/\tTRUE\t0\t{name_}\t{value}\n")
        return Path(name)

    cookie_file = _cookie_file_path()
    try:
        params: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "flat_playlist": True,   # header-only enumeration, no segment work
            "extract_flat": "in_playlist",  # yt-dlp 2025.1+: per-entry ids, no resolve
            "playlist_items": "1:100000",
            "noprogress": True,
            "ignoreerrors": True,     # one dead entry must not kill the whole list
        }
        if cookie_file is not None:
            params["cookiefile"] = str(cookie_file)
        if headers:
            params["http_headers"] = dict(headers)
        from magnetoclip.media.streaming import _apply_impersonation, _apply_manifest_token

        # Reuse the streaming engine's token/impersonation application so a
        # signed or bot-walled manifest reference gets the same treatment here.
        _apply_manifest_token(params, playlist_url)
        _apply_impersonation(params)

        with YoutubeDL(params) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
        entries = (info or {}).get("entries") or []
        urls: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id") or entry.get("video_id")
            # yt-dlp flat entries may also carry a resolved ``url``.
            direct = entry.get("url")
            if entry_id:
                candidate = _watch_url(str(entry_id))
            elif direct and str(direct).startswith("http"):
                candidate = str(direct)
            else:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
            if limit is not None and len(urls) >= limit:
                break
        return urls
    except Exception as exc:  # noqa: BLE001 - enumeration is best-effort
        log.warning("playlist_expand_failed url=%s error=%s", playlist_url, str(exc)[:200])
        return []
    finally:
        if cookie_file is not None:
            try:
                cookie_file.unlink(missing_ok=True)
            except OSError:
                pass


def build_playlist_download_error(playlist_url: str, reason: str) -> str:
    """A single, human line for the failure UI when a playlist expands to
    zero videos (caller decides whether to fail the whole row)."""
    return f"playlist contains no downloadable videos: {reason}"


__all__ = [
    "is_playlist_url",
    "extract_playlist_video_urls",
    "build_playlist_download_error",
    "_is_youtube",
    "_host_of",
]
