"""Streaming / embedded media support via yt-dlp extractors.

Detects whether a URL points to a streaming platform (YouTube, Vimeo, SoundCloud,
...), resolves metadata, and downloads media through yt-dlp while feeding
progress events back into the normal MagnetoClip download pipeline.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Fast host-based detection. yt-dlp's extractor scan over every IE is ~700ms per
# URL, which is far too slow to run on every add(), so we short-circuit with a
# curated list first and only fall back to the full extractor scan when the host
# is unknown.
STREAMING_HOST_SUFFIXES: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vimeo.com",
    "player.vimeo.com",
    "dailymotion.com",
    "dai.ly",
    "twitch.tv",
    "soundcloud.com",
    "snd.click",
    "tiktok.com",
    "vm.tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
    "bilibili.com",
    "b23.tv",
    "vimeo.com",
    "bandcamp.com",
    "app.bandcamp.com",
    "mixcloud.com",
    "spotify.com",
    "open.spotify.com",
    "deezer.com",
    "music.youtube.com",
    "m3u8",
    "mpd",
)

AUDIO_ONLY_HOST_SUFFIXES: tuple[str, ...] = (
    "soundcloud.com",
    "snd.click",
    "bandcamp.com",
    "app.bandcamp.com",
    "mixcloud.com",
    "spotify.com",
    "open.spotify.com",
    "deezer.com",
    "music.youtube.com",
)

_MANIFEST_RE = re.compile(r"\.(m3u8|mpd)(\?|#|$)", re.IGNORECASE)


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_bot_wall_error(message: str) -> bool:
    """True when yt-dlp reports an auth/bot-wall block that browser cookies may help with."""
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "sign in to confirm",
            "not a bot",
            "recaptcha",
            "unusual traffic",
            "request has been throttled",
            "too many requests",
        )
    )


def is_streaming_url(url: str) -> bool:
    """Return True if *url* points to an embedded/streaming media platform."""
    if not url or not isinstance(url, str):
        return False
    host = _host_of(url)
    if any(host == s or host.endswith("." + s) for s in STREAMING_HOST_SUFFIXES if "." in s):
        return True
    return bool(_MANIFEST_RE.search(url))


def is_audio_platform(url: str) -> bool:
    host = _host_of(url)
    return any(host == s or host.endswith("." + s) for s in AUDIO_ONLY_HOST_SUFFIXES if "." in s)


class DownloadCancelled(Exception):
    """Raised inside yt-dlp hooks to abort the download."""


class StreamResolutionError(Exception):
    """Raised when yt-dlp cannot resolve the URL."""


@dataclass
class StreamInfo:
    url: str
    title: str
    ext: str
    media_type: str  # "video" | "audio"
    size: int | None
    duration: float | None
    webpage_url: str | None
    formats: list[dict] = field(default_factory=list)


def _sanitize_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_len] if name else "stream"


def _ffmpeg_available() -> bool:
    import shutil

    return shutil.which("ffmpeg") is not None


def _write_cookiefile(url: str, cookies) -> Path | None:
    """Write ``cookies`` (dict or header string) to a Netscape cookie file.

    yt-dlp loads cookies through its cookie jar, which authenticates
    browser-gated platforms like YouTube (the "Sign in to confirm you're not
    a bot" wall). Returns the temp file path or None when there is nothing
    to write. The caller is responsible for deleting the file.
    """
    if not cookies:
        return None
    from tempfile import NamedTemporaryFile

    from ..network.cookies.jar import parse_cookie_header

    if isinstance(cookies, str):
        cookies = parse_cookie_header(cookies)
    if not cookies:
        return None
    host = _host_of(url)
    secure = url.startswith("https://")
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append(
            f".{host}\tTRUE\t/\t{'TRUE' if secure else 'FALSE'}\t2147483647\t{name}\t{value}"
        )
    handle = NamedTemporaryFile(
        "w", suffix=".cookies.txt", encoding="utf-8", delete=False
    )
    try:
        handle.write("\n".join(lines))
    finally:
        handle.close()
    return Path(handle.name)


def _build_format_selector(quality: str, media_type: str) -> str:
    """Build a yt-dlp -f expression for the requested quality.

    Without ffmpeg we cannot merge video+audio tracks, so we restrict ourselves
    to single-file progressive formats and let yt-dlp pick the next best match.
    """
    audio_only = media_type == "audio" or quality == "audio"
    if audio_only:
        return "bestaudio/best"
    merged = "+"
    if not _ffmpeg_available():
        merged = ""
        suffix = "[acodec!=none][vcodec!=none]"
    else:
        suffix = ""
    if quality == "1080":
        sel = f"bestvideo*{suffix}[height<=1080]{merged}bestaudio/best{suffix}[height<=1080]/best"
    elif quality == "720":
        sel = f"bestvideo*{suffix}[height<=720]{merged}bestaudio/best{suffix}[height<=720]/best"
    else:
        sel = f"best{suffix}/best"
    return sel


def resolve_stream(
    url: str,
    quality: str = "best",
    timeout: float = 20.0,
    cookies=None,
) -> StreamInfo:
    """Resolve metadata for *url* without downloading. Raises StreamResolutionError."""
    from yt_dlp import YoutubeDL

    selector = _build_format_selector(quality, "video")
    base_params = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": timeout,
        "nocheckcertificate": True,
        "format": selector,
    }
    # Try anonymously first: passing browser cookies can trigger a degraded
    # player response for some sessions. Only retry with cookies when the
    # anonymous attempt is blocked by an auth/bot wall.
    info = None
    for attempt_cookies in ((None,) if not cookies else (None, cookies)):
        cookiefile = _write_cookiefile(url, attempt_cookies)
        params = dict(base_params)
        if cookiefile is not None:
            params["cookiefile"] = str(cookiefile)
        try:
            try:
                with YoutubeDL(params) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:
                message = str(exc)
                can_retry = (
                    attempt_cookies is None
                    and bool(cookies)
                    and _is_bot_wall_error(message)
                )
                if not can_retry:
                    logger.warning("Failed to resolve stream for %s: %s", url, message)
                    raise StreamResolutionError(message) from exc
                continue
        finally:
            if cookiefile is not None:
                try:
                    cookiefile.unlink(missing_ok=True)
                except OSError:
                    pass
        break

    if not info:
        raise StreamResolutionError("yt-dlp returned no info")

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise StreamResolutionError("Playlist contains no entries")
        info = entries[0]

    media_type = "audio" if (is_audio_platform(url) or info.get("vcodec") == "none") else "video"
    ext = info.get("ext") or "mp4"
    return StreamInfo(
        url=url,
        title=_sanitize_filename(str(info.get("title") or "stream")),
        ext=ext,
        media_type=media_type,
        size=info.get("filesize") or info.get("filesize_approx"),
        duration=info.get("duration"),
        webpage_url=info.get("webpage_url") or url,
        formats=info.get("formats") or [],
    )


def download_stream(
    url: str,
    save_dir: Path,
    quality: str = "best",
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    timeout: float = 20.0,
    cookies=None,
) -> Path:
    """Download *url* into *save_dir*, returning the final file path.

    *progress_cb* is invoked with a dict of yt-dlp progress info. If
    *cancel_event* is set at any point the download is aborted and the partial
    file is removed. *cookies* (dict or header string) are written to a
    temporary Netscape cookie file so browser-authenticated sources resolve.
    """
    from yt_dlp import YoutubeDL

    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    # media_type is unknown before resolution; "video" lets the selector fall
    # back to bestaudio/best for audio-only sources.
    selector = _build_format_selector(quality, "video")

    outtmpl = str(save_dir / "%(title)s.%(ext)s")

    hooks = []

    def _progress_hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("cancelled")
        if progress_cb is not None:
            progress_cb(d)

    hooks.append(_progress_hook)

    base_params = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": timeout,
        "nocheckcertificate": True,
        "outtmpl": {"default": outtmpl, "pl_thumbnail": outtmpl},
        "format": selector,
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": hooks,
    }
    if not _ffmpeg_available():
        base_params["merge_output_format"] = None

    # Anonymous first, cookies as a bot-wall fallback (see resolve_stream).
    info = None
    for attempt_cookies in ((None,) if not cookies else (None, cookies)):
        cookiefile = _write_cookiefile(url, attempt_cookies)
        params = dict(base_params)
        if cookiefile is not None:
            params["cookiefile"] = str(cookiefile)
        try:
            try:
                with YoutubeDL(params) as ydl:
                    info = ydl.extract_info(url, download=True)
            except DownloadCancelled:
                raise
            except Exception as exc:
                message = str(exc)
                can_retry = (
                    attempt_cookies is None
                    and bool(cookies)
                    and _is_bot_wall_error(message)
                )
                if not can_retry:
                    logger.warning("Stream download failed for %s: %s", url, message)
                    raise
                continue
        finally:
            if cookiefile is not None:
                try:
                    cookiefile.unlink(missing_ok=True)
                except OSError:
                    pass
        break

    if not info:
        raise StreamResolutionError("yt-dlp returned no info")

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else info

    requested = (info.get("requested_downloads") or [])
    final_path = None
    if requested:
        final_path = requested[0].get("filepath")
    if not final_path:
        final_path = ydl.prepare_filename(info)
    if not final_path:
        candidates = list(save_dir.iterdir())
        if candidates:
            final_path = str(candidates[-1])

    return Path(final_path)
