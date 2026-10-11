"""Header-only YouTube playlist enumeration (``media/youtube/engine.py``).

This is the *preview* engine: it resolves a playlist URL to its entries using
yt-dlp ``flat`` extraction, which fetches only the playlist metadata (ids,
titles, durations) and **never downloads any media**. Each selected entry is
later downloaded by the existing per-video pipeline by canonical watch URL.

Transport mirrors :mod:`magnetoclip.media.streaming` (cookiefile / headers /
impersonation), so a bot-walled or signed link behaves the same way as in the
rest of the app. Any failure is reported as a user-facing
:class:`PlaylistExtractionError` instead of silently dropping entries, so the
UI can show *why* the preview is empty.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable

from magnetoclip.services.logging import get_logger

from .detector import classify
from .types import PlaylistVideo, YouTubePlaylist, YouTubeURLType

log = get_logger(__name__)


class PlaylistExtractionError(Exception):
    """A playlist could not be enumerated for a user-facing reason."""


class PlaylistExtractionCancelled(PlaylistExtractionError):
    """The extraction was cancelled by the user (or the app is shutting down)."""


_PLAYLIST_KINDS = (YouTubeURLType.playlist, YouTubeURLType.video_with_playlist)


class YouTubePlaylistEngine:
    """Enumerates playlist entries with yt-dlp flat extraction.

    The engine is deliberately stateless and import-free of Qt: tests and the
    ``QThread`` worker all call the same :meth:`extract`.
    """

    def __init__(self, *, max_entries: int = 100_000) -> None:
        self.max_entries = max_entries

    # ── public API ────────────────────────────────────────────────────────

    def extract(
        self,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        limit: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> YouTubePlaylist:
        """Resolve *url* to a :class:`YouTubePlaylist` without downloading.

        *cookies*/*headers* are forwarded to yt-dlp (same handling as
        :func:`magnetoclip.media.streaming.resolve_stream`). *limit* caps how
        many entries are returned (useful for very long mixes); ``None``
        returns them all. *cancelled* is polled between entries and, when it
        returns ``True``, aborts with :class:`PlaylistExtractionCancelled`.

        Raises :class:`PlaylistExtractionError` when the URL is not a playlist
        or the playlist yields no usable entries.
        """
        if not url or not isinstance(url, str):
            raise PlaylistExtractionError("not a valid URL")
        kind = classify(url)
        if kind not in _PLAYLIST_KINDS:
            raise PlaylistExtractionError("not a YouTube playlist URL")

        if cancelled and cancelled():
            raise PlaylistExtractionCancelled("cancelled")

        cookie_file = _write_cookie_file(cookies)
        try:
            params: dict = self._params(cookie_file, headers)
            info = self._extract_info(url, params)
        finally:
            _unlink(cookie_file)

        if cancelled and cancelled():
            raise PlaylistExtractionCancelled("cancelled")

        if not isinstance(info, dict):
            raise PlaylistExtractionError("playlist could not be resolved")

        entries = info.get("entries")
        playlist_id = str(info.get("id") or _playlist_id_from(url) or "playlist")
        title = str(info.get("title") or "Untitled playlist")

        if not isinstance(entries, (list, tuple)):
            # A bare watch URL resolved a single video (e.g. the caller asked
            # to preview a ``watch?v=…&list=…`` the playlist of which is gone).
            videos = self._videos_from(info, cancelled=cancelled)
        else:
            videos = self._videos_from_entries(list(entries), cancelled=cancelled)

        if not videos:
            raise PlaylistExtractionError(
                "playlist contains no downloadable videos"
            )
        if limit is not None:
            videos = videos[:limit]

        return YouTubePlaylist(
            playlist_id=playlist_id,
            title=title,
            url=_canonical_playlist_url(playlist_id),
            video_count=len(videos),
            videos=videos,
        )

    # ── internals ─────────────────────────────────────────────────────────

    def _params(self, cookie_file: Path | None, headers: dict[str, str] | None) -> dict:
        params: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "flat_playlist": True,  # header-only enumeration, no segment work
            "extract_flat": "in_playlist",  # per-entry ids, no full resolve
            "playlist_items": f"1:{self.max_entries}",
            "noprogress": True,
            "ignoreerrors": True,  # one dead entry must not kill the whole list
        }
        if cookie_file is not None:
            params["cookiefile"] = str(cookie_file)
        if headers:
            params["http_headers"] = dict(headers)
        return params

    def _extract_info(self, url: str, params: dict) -> dict:
        # Reuse the streaming engine's impersonation/token setup so signed or
        # bot-walled playlist references get identical treatment.
        from magnetoclip.media.streaming import _apply_impersonation, _apply_manifest_token

        _apply_manifest_token(params, url)
        _apply_impersonation(params)

        from yt_dlp import YoutubeDL

        try:
            with YoutubeDL(params) as ydl:
                info = ydl.extract_info(url, download=False)
        except PlaylistExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            log.warning("playlist_extract_failed url=%s error=%s", url, str(exc)[:200])
            raise PlaylistExtractionError(f"playlist could not be fetched: {exc}") from exc
        return info

    def _videos_from_entries(
        self,
        entries: list,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> list[PlaylistVideo]:
        videos: list[PlaylistVideo] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries, start=1):
            if cancelled and cancelled():
                raise PlaylistExtractionCancelled("cancelled")
            if not isinstance(entry, dict):
                continue
            video_id = entry.get("id") or entry.get("video_id")
            direct = entry.get("url")
            if not video_id:
                continue
            video_id = str(video_id)
            if video_id in seen:
                continue
            seen.add(video_id)
            title = str(entry.get("title") or f"Video {index}")
            duration = _as_float(entry.get("duration"))
            videos.append(
                PlaylistVideo(
                    video_id=video_id,
                    title=title,
                    url=_canonical_watch_url(video_id),
                    index=index,
                    duration=duration,
                )
            )
        return videos

    def _videos_from(
        self,
        info: dict,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> list[PlaylistVideo]:
        if cancelled and cancelled():
            raise PlaylistExtractionCancelled("cancelled")
        video_id = info.get("id") or info.get("video_id")
        if not video_id:
            return []
        video_id = str(video_id)
        return [
            PlaylistVideo(
                video_id=video_id,
                title=str(info.get("title") or video_id),
                url=_canonical_watch_url(video_id),
                index=1,
                duration=_as_float(info.get("duration")),
            )
        ]


# ── helpers ────────────────────────────────────────────────────────────────


def _canonical_watch_url(video_id: str) -> str:
    return "https://www.youtube.com/watch?v=" + video_id


def _canonical_playlist_url(playlist_id: str) -> str:
    return "https://www.youtube.com/playlist?list=" + playlist_id


def _playlist_id_from(url: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    return (query.get("list") or [None])[0]


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_cookie_file(cookies: dict[str, str] | None) -> Path | None:
    if not cookies:
        return None
    fd, name = tempfile.mkstemp(suffix=".txt")
    with __import__("os").fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for key, value in (cookies or {}).items():
            fh.write(f".youtube.com\tTRUE\t/\tTRUE\t0\t{key}\t{value}\n")
    return Path(name)


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "PlaylistExtractionCancelled",
    "PlaylistExtractionError",
    "YouTubePlaylistEngine",
]