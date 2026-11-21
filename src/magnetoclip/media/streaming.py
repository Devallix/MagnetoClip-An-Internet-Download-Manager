"""Streaming / embedded media support via yt-dlp extractors.

Detects whether a URL points to a streaming platform (YouTube, Vimeo, SoundCloud,
...), resolves metadata, and downloads media through yt-dlp while feeding
progress events back into the normal MagnetoClip download pipeline.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
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

# Streaming sites that serve HLS/DASH video but may not have yt-dlp extractors.
# The browser extension detects m3u8/mpd manifests on these sites and offers
# the page URL; yt-dlp's generic extractor then attempts to resolve the stream.
STREAMING_SITE_HOSTS: tuple[str, ...] = (
    "anikai.watch",
    "anikai.vip",
    "anikai.rip",
    "anikai.to",
    "animekai.com.ro",
    "animekai.org.lv",
    "hdtoday24.at",
    "hdtodayz.app",
    "hdtoday.duckdns.org",
    "hdtoday.to",
    "4thdmovie.com",
    "yflix.in",
    "flaxfer.lol",
    "hianimes.se",
    # Player/playlist hosts observed in browser captures. These are useful for
    # pasted page URLs too; opaque playlist endpoints are routed by the stronger
    # RequestContext media-kind hint rather than by modifying their signed URL.
    "megaplays.se",
    "cinesrc.st",
    "coralorbit.top",
)

_MANIFEST_RE = re.compile(
    r"\.(m3u8|mpd)(?:[?#]|$)|[?&](?:format|ext)=m3u8(?:&|$)|[?&](?:format|ext)=mpd(?:&|$)",
    re.IGNORECASE,
)


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_bot_wall_error(message: str) -> bool:
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
            "403",
            "forbidden",
        )
    )


def _impersonation_available() -> bool:
    """True when yt-dlp's browser-impersonation dependency (curl-cffi) is installed."""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


def _apply_manifest_token(params: dict, url: str) -> None:
    """Make yt-dlp's generic extractor carry the manifest query (e.g. ``token``)
    onto every HLS child request, mirroring :func:`browser_streaming._resolve_hls_url`.

    Tokenized CDNs sign the master playlist URL with ``?token=...`` and require
    that same token on variant, segment and key requests. yt-dlp's generic
    extractor resolves relative HLS references with ``urljoin`` (which drops the
    base query) — but its ``variant_query``/``fragment_query``/``key_query``
    extractor args re-append an arbitrary query string to variant manifest URLs,
    segment URLs and EXT-X-KEY URIs respectively. When *url* is an HLS/DASH
    manifest with a query string, we seed those args with it. No-op otherwise.
    """
    if not is_hls_or_dash_url(url):
        return
    from urllib.parse import urlparse

    query = urlparse(url).query
    if not query:
        return
    generic = params.setdefault("extractor_args", {}).setdefault("generic", {})
    generic.setdefault("variant_query", [query])
    generic.setdefault("fragment_query", [query])
    generic.setdefault("key_query", [query])


def _is_youtube_url(url: str) -> bool:
    """Return True for YouTube URLs handled by yt-dlp's YouTube extractor."""
    host = _host_of(url)
    return host in {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com",
        "youtu.be", "www.youtu.be",
    } or host.endswith(".youtube.com")


def _apply_impersonation(params: dict) -> None:
    """Enable yt-dlp browser impersonation on *params* in place.

    Cloudflare-protected stream hosts (common on streaming sites such as anikai
    and hdtoday) reject plain HTTP requests with a ``403 anti-bot challenge``.
    yt-dlp only answers that when impersonating a real browser via curl-cffi;
    both the resolution pass and the HLS fragment download need it enabled.
    Merges the generic extractor args so existing keys (e.g. youtube) survive.
    No-op when curl-cffi is not installed.
    """
    # Browser impersonation is retained for Cloudflare-protected generic/HLS
    # sites, but is not forced onto YouTube. YouTube client selection and its
    # current token requirements should remain under yt-dlp's control.
    if not _impersonation_available() or _is_youtube_url(params.get("_magnetoclip_url", "")):
        return
    from yt_dlp.networking.impersonate import ImpersonateTarget

    params["impersonate"] = ImpersonateTarget.from_str("chrome")
    extractor_args = params.setdefault("extractor_args", {})
    extractor_args["generic"] = {"impersonate": ["chrome"]}


# YouTube player clients to fall back on when a session is blocked from the
# default web chain. From the datacenter IPs used while developing this, the
# web-family clients return "Requested format is not available" for most videos,
# and a mid-stream ``HTTP Error 403`` throttles the anonymous media URL after a
# few MB. ``android_vr`` exposes the full ladder (up to 8K) and is served from
# nodes that are far less aggressively gated; ``tv``/``ios`` are extra fallbacks.
_YOUTUBE_CLIENT_FALLBACKS: tuple[str, ...] = ()


def _set_player_client(params: dict, client: str) -> None:
    """Pin the YouTube player client on *params* in place.

    Only used for fallback attempts (the first attempt keeps yt-dlp's default
    chain). Merges onto existing extractor args so impersonation survives.
    """
    extractor_args = params.setdefault("extractor_args", {})
    youtube = extractor_args.setdefault("youtube", {})
    youtube["player_client"] = [client]


def _is_stream_retryable_error(message: str) -> bool:
    """Return True when *message* is worth a fresh-extract retry.

    Covers bot/flow walls (Cloudflare 403, YouTube "sign in to confirm"),
    per-client blocks (``Requested format is not available`` / "No video
    formats found"), and YouTube's mid-stream ``HTTP Error 403`` on the media
    URL, which is transient (fresh signed URLs from a different client/CDN node
    frequently succeed).
    """
    lowered = (message or "").lower()
    return (
        is_bot_wall_error(message)
        or "requested format is not available" in lowered
        or "no video formats found" in lowered
        or "unable to download video data" in lowered
        or "http error 403" in lowered
        or "the page needs to be reloaded" in lowered
    )


def _youtube_attempts(*, url: str, cookies, browser_name, for_download: bool):
    """Yield safe retry attempts without reading unrelated browser profiles.

    Captured request cookies are valid for any provider and are tried when
    supplied.  Reading a whole browser cookie database is a separate, fragile
    operation (Chrome may lock or app-bound-encrypt its database while running),
    so it is reserved for YouTube URLs where that fallback is useful.  Generic
    HLS/DASH streaming sites must use the request cookies/headers captured by
    the extension and must never fail because Chrome's cookie DB cannot be
    copied.

    Current YouTube client/PO-token requirements change over time. MagnetoClip
    therefore leaves player-client selection to the installed yt-dlp version
    instead of pinning legacy clients such as ``android_vr``.
    """
    base = [(None, None)]
    if cookies:
        base.append((None, cookies))
    if browser_name and _is_youtube_url(url):
        base.append((None, "_browser_"))
    # Keep retries bounded.  Fresh YoutubeDL instances re-extract fresh media
    # URLs rather than rotating to legacy clients that may require PO tokens.
    if for_download:
        return base + [(None, None)] * 2
    return base


def _backoff_before_attempt(index: int) -> float:
    """Seconds to wait before starting attempt ``index`` (0-based, >=1).

    Grow a short backoff so a throttled YouTube session has time to recover,
    while keeping downloads responsive: 1s, 2s, 4s, 8s... capped at 25s.
    """
    if index <= 0:
        return 0.0
    return min(2 ** (index - 1), 25.0)


def is_streaming_url(url: str) -> bool:
    """Return True if *url* points to an embedded/streaming media platform."""
    if not url or not isinstance(url, str):
        return False
    host = _host_of(url)
    if any(host == s or host.endswith("." + s) for s in STREAMING_HOST_SUFFIXES if "." in s):
        return True
    if is_streaming_site_url(url):
        return True
    return bool(_MANIFEST_RE.search(url))


def is_streaming_site_url(url: str) -> bool:
    """Return True if *url* is from a known streaming site (anime/movie sites)."""
    if not url or not isinstance(url, str):
        return False
    host = _host_of(url)
    return any(host == s or host.endswith("." + s) for s in STREAMING_SITE_HOSTS if "." in s)


def is_hls_url(url: str) -> bool:
    """Return True when *url* points to an HLS M3U8 manifest URL."""
    if not url or not isinstance(url, str):
        return False
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    path_extension = re.search(r"\.([a-z0-9]{2,8})$", path, re.IGNORECASE)
    if path_extension:
        return path_extension.group(1).lower() == "m3u8"
    return bool(re.search(r"[?&](?:format|ext)=m3u8(?:&|$)", url, re.IGNORECASE))


def is_hls_or_dash_url(url: str) -> bool:
    """Return True if *url* points to an HLS (.m3u8) or DASH (.mpd) manifest."""
    if not url or not isinstance(url, str):
        return False
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

    def to_media_info(self, *, context=None):
        """Convert this yt-dlp result into the canonical :class:`MediaInfo`.

        ``StreamInfo`` remains the yt-dlp-facing shape; the rest of the
        pipeline consumes the canonical model returned here.
        """
        from .models import MediaInfo

        return MediaInfo.from_ydl(
            self.url,
            title=self.title,
            ext=self.ext,
            media_type=self.media_type,
            size=self.size,
            duration=self.duration,
            webpage_url=self.webpage_url,
            formats=self.formats,
            context=context,
        )


def _sanitize_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_len] if name else "stream"


def _ffmpeg_available(settings=None) -> bool:
    """Return whether MagnetoClip can resolve a usable FFmpeg executable.

    Prefer the configured/bundled executable before falling back to PATH.
    Import locally to avoid a module-import cycle: ffmpeg.py imports
    DownloadCancelled from this module.
    """
    try:
        from .ffmpeg import FFmpegLocator

        locator = FFmpegLocator(settings=settings)
        return locator.ffmpeg_available
    except Exception:
        # Keep download selection resilient if locator initialization itself
        # fails; PATH remains a last-resort compatibility fallback.
        import shutil

        return shutil.which("ffmpeg") is not None


def _ffmpeg_executable(settings=None) -> str | None:
    """Return the exact FFmpeg executable path for yt-dlp, if available.

    yt-dlp does not automatically use MagnetoClip's bundled FFmpeg. Supplying
    this path explicitly is required on clean Windows machines where FFmpeg is
    not installed globally or added to PATH.
    """
    try:
        from .ffmpeg import FFmpegLocator

        locator = FFmpegLocator(settings=settings)
        return locator.ffmpeg() if locator.ffmpeg_available else None
    except Exception:
        import shutil

        return shutil.which("ffmpeg")


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


def _build_format_selector(
    quality: str,
    media_type: str,
    url: str | None = None,
    manifest_kind: str | None = None,
) -> str:
    """Build a yt-dlp format selector for the requested quality.

    Ordinary site extractors generally expose explicit ``vcodec``/``acodec``
    values, so those downloads can safely prefer separate best-video and
    best-audio streams and require a progressive audio+video fallback when
    FFmpeg is unavailable.

    Generic HLS/DASH manifests are different: yt-dlp's Generic extractor often
    cannot infer codec metadata from a master playlist (especially when its
    ``CODECS`` attribute is omitted). Filters such as ``[acodec!=none]`` and
    ``[vcodec!=none]`` then reject every otherwise playable manifest format,
    producing ``Requested format is not available``. For manifest URLs use
    yt-dlp's standard manifest-compatible selector without codec filters. The
    ``best`` fallback is a combined audio+video format, not a video-only
    format; without FFmpeg we intentionally require that single-file option.
    """
    audio_only = media_type == "audio" or quality == "audio"
    if audio_only:
        return "bestaudio/best"

    ffmpeg_available = _ffmpeg_available()
    is_manifest = bool(
        (url and is_hls_or_dash_url(url))
        or str(manifest_kind or "").strip().lower() in {"hls", "dash", "stream"}
    )
    height_match = re.search(r"(\d{3,4})", str(quality or ""))
    height_cap = int(height_match.group(1)) if height_match else None

    if is_manifest:
        if ffmpeg_available:
            if height_cap:
                return (
                    f"bestvideo[height<={height_cap}]+bestaudio/"
                    f"best[height<={height_cap}]/bestvideo+bestaudio/best"
                )
            return "bestvideo+bestaudio/best"
        if height_cap:
            return f"best[height<={height_cap}]/best"
        return "best"

    if quality == "1080":
        progressive = "best[acodec!=none][vcodec!=none][height<=1080]"
        sel = (
            f"bestvideo[height<=1080]+bestaudio/{progressive}"
            if ffmpeg_available else progressive
        )
    elif quality == "720":
        progressive = "best[acodec!=none][vcodec!=none][height<=720]"
        sel = (
            f"bestvideo[height<=720]+bestaudio/{progressive}"
            if ffmpeg_available else progressive
        )
    else:
        progressive = "best[acodec!=none][vcodec!=none]"
        sel = (
            f"bestvideo+bestaudio/{progressive}"
            if ffmpeg_available else progressive
        )
    return sel


def resolve_stream(
    url: str,
    quality: str = "best",
    timeout: float = 20.0,
    cookies=None,
    headers: dict | None = None,
    format_selector: str | None = None,
    merge_output_format: str | None = None,
    manifest_kind: str | None = None,
) -> StreamInfo:
    """Resolve metadata for *url* without downloading. Raises StreamResolutionError.

    *format_selector* overrides the quality-derived ``-f`` expression (used by
    the YouTube flow where the selector was chosen against the real ladder).
    *manifest_kind* carries response-derived HLS/DASH evidence for opaque
    playlist URLs that do not end in ``.m3u8`` or ``.mpd``.
    """
    from yt_dlp import YoutubeDL

    if format_selector is None:
        format_selector = _build_format_selector(
            quality, "video", url=url, manifest_kind=manifest_kind
        )
    selector = format_selector
    base_params = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": timeout,
        "nocheckcertificate": True,
        "format": selector,
        "_magnetoclip_url": url,
    }
    ffmpeg_executable = _ffmpeg_executable()
    if ffmpeg_executable:
        base_params["ffmpeg_location"] = ffmpeg_executable
    if merge_output_format:
        base_params["merge_output_format"] = merge_output_format
    _apply_impersonation(base_params)
    _apply_manifest_token(base_params, url)
    if headers:
        base_params["http_headers"] = dict(headers)
    # Use yt-dlp's default client chain (same as the metadata worker, which
    # feeds the quality dialog) so the resolved/selected ladder matches what
    # the user picked. Forcing the ``android`` player client here used to
    # dodge bot-walls but now caps YouTube to 360p: high resolutions are only
    # served to web clients.

    # Try anonymously first. Captured request cookies are the preferred
    # provider-independent fallback; reading a browser profile is only enabled
    # for YouTube, never for generic HLS/DASH streaming sites.
    info = None
    browser_name = _detect_browser() if _is_youtube_url(url) else None
    last_error = "yt-dlp returned no info"
    for client, attempt_cookies in _youtube_attempts(
        url=url,
        cookies=cookies,
        browser_name=browser_name,
        for_download=False,
    ):
        use_cookies = attempt_cookies if attempt_cookies != "_browser_" else None
        cookiefile = _write_cookiefile(url, use_cookies)
        params = copy.deepcopy(base_params)
        if client:
            _set_player_client(params, client)
        if cookiefile is not None:
            params["cookiefile"] = str(cookiefile)
        elif attempt_cookies == "_browser_" and browser_name:
            params["cookiesfrombrowser"] = (browser_name,)
            logger.info(
                "Using browser cookie jar (%s) for %s", browser_name, url
            )
        try:
            try:
                params.pop("_magnetoclip_url", None)
                with YoutubeDL(params) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:
                message = str(exc)
                if attempt_cookies == "_browser_":
                    # Cookie-read failures (e.g. Chrome app-bound encryption
                    # while the browser is running) are environment limits, not
                    # media errors: skip to the next attempt.
                    logger.warning(
                        "Browser cookie attempt failed for %s: %s", url, message
                    )
                    # Do not overwrite the actual stream/extractor failure
                    # with a local browser-profile read error. Browser cookie
                    # extraction is optional and must never mask a media error.
                    continue
                if not _is_stream_retryable_error(message):
                    logger.warning("Failed to resolve stream for %s: %s", url, message)
                    raise StreamResolutionError(message) from exc
                last_error = message
                logger.warning(
                    "Retrying stream resolve for %s (client=%s): %s", url, client, message
                )
                continue
        finally:
            if cookiefile is not None:
                try:
                    cookiefile.unlink(missing_ok=True)
                except OSError:
                    pass
        break

    if not info:
        raise StreamResolutionError(last_error)

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


def _detect_browser() -> str | None:
    """Return first installed browser name for yt-dlp cookiesfrombrowser, or None.

    Checks both PATH and common Windows install directories.
    """
    import os
    import shutil

    candidates = [
        ("chrome", "chrome.exe"),
        ("edge", "msedge.exe"),
        ("firefox", "firefox.exe"),
        ("brave", "brave.exe"),
        ("opera", "opera.exe"),
        ("vivaldi", "vivaldi.exe"),
    ]
    # Real Windows installs put the browser binary *below* the vendor folder
    # (``Google\Chrome\Application\chrome.exe``, ``Microsoft\Edge\Application\
    # msedge.exe``, ``BraveSoftware\Brave-Browser\Application\brave.exe`` …),
    # so also probe one level deeper than the top-level exe check above.
    nested = {
        "chrome": (r"Google\Chrome\Application\chrome.exe",),
        "edge": (r"Microsoft\Edge\Application\msedge.exe",),
        "brave": (r"BraveSoftware\Brave-Browser\Application\brave.exe",),
        "opera": (r"Opera Software\Application\opera.exe",),
        "vivaldi": (r"Vivaldi\Application\vivaldi.exe",),
    }
    local_app = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", "")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
    extra_dirs = []
    for base in (local_app, program_files, program_files_x86):
        if base:
            extra_dirs.append(base)
            extra_dirs.append(os.path.join(base, "Google"))
            extra_dirs.append(os.path.join(base, "Microsoft"))
            extra_dirs.append(os.path.join(base, "BraveSoftware"))
            extra_dirs.append(os.path.join(base, "Opera Software"))
            extra_dirs.append(os.path.join(base, "Vivaldi"))

    for name, exe in candidates:
        if shutil.which(exe):
            return name
        for d in extra_dirs:
            path = os.path.join(d, exe)
            if os.path.isfile(path):
                return name
        for d in extra_dirs:
            for rel in nested.get(name, ()):
                path = os.path.join(d, rel)
                if os.path.isfile(path):
                    return name
    return None


def download_stream(
    url: str,
    save_dir: Path,
    quality: str = "best",
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    timeout: float = 20.0,
    cookies=None,
    headers: dict | None = None,
    filename: str | None = None,
    format_selector: str | None = None,
    merge_output_format: str | None = None,
    manifest_kind: str | None = None,
) -> Path:
    """Download *url* into *save_dir*, returning the final file path.

    *progress_cb* is invoked with a dict of yt-dlp progress info. If
    *cancel_event* is set at any point the download is aborted and the partial
    file is removed. *cookies* (dict or header string) are written to a
    temporary Netscape cookie file so browser-authenticated sources resolve.
    *headers* (e.g. a Referer captured from the player page) are passed to
    yt-dlp as extra request headers. When *filename* is given it is used
    verbatim as the output path (instead of a title-derived template), which
    lets a paused download continue appending to its existing ``.part`` file.
    *format_selector* overrides the quality-derived ``-f`` expression and
    *merge_output_format* pins the merged container: both are used by the
    YouTube flow, which resolved the real ladder before queuing.
    """
    from yt_dlp import YoutubeDL

    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    # media_type is unknown before resolution; "video" lets the selector fall
    # back to bestaudio/best for audio-only sources.
    if format_selector is None:
        format_selector = _build_format_selector(
            quality, "video", url=url, manifest_kind=manifest_kind
        )
    selector = format_selector
    # Safety net: a merge selector (bv+ba) chosen while ffmpeg existed cannot
    # work if ffmpeg vanished since then. Degrade only to a progressive format
    # that explicitly contains BOTH audio and video; otherwise fail rather than
    # silently producing a mute video.
    if "+" in selector and not _ffmpeg_available():
        height_cap = re.search(r"\[height<=(\d+)\]", selector)
        manifest_selection = bool(
            (url and is_hls_or_dash_url(url))
            or str(manifest_kind or "").strip().lower() in {"hls", "dash", "stream"}
        )
        if manifest_selection:
            # Generic manifest formats may omit codec metadata entirely, so
            # retain yt-dlp's combined A/V alias instead of adding codec filters
            # that would reject every HLS variant.
            selector = f"best[height<={height_cap.group(1)}]/best" if height_cap else "best"
        elif height_cap:
            selector = f"b[acodec!=none][vcodec!=none][height<={height_cap.group(1)}]"
        else:
            selector = "b[acodec!=none][vcodec!=none]"
        logger.warning(
            "ffmpeg unavailable; degraded merge selector to single-file: %s",
            selector,
        )

    if filename:
        outtmpl = str(save_dir / filename)
    else:
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
        "concurrent_fragment_downloads": 1 if _is_youtube_url(url) else 4,
        "progress_hooks": hooks,
        "_magnetoclip_url": url,
    }
    # A forced chunk size can interact poorly with YouTube's signed media URLs.
    # Preserve the existing chunked behaviour for other streaming providers.
    if not _is_youtube_url(url):
        base_params["http_chunk_size"] = 5 * 1024 * 1024
    _apply_impersonation(base_params)
    _apply_manifest_token(base_params, url)
    if headers:
        base_params["http_headers"] = dict(headers)
    ffmpeg_executable = _ffmpeg_executable()
    if ffmpeg_executable:
        # Explicitly point yt-dlp at the bundled executable; PATH lookup alone
        # fails on first-time installs on machines without system FFmpeg.
        base_params["ffmpeg_location"] = ffmpeg_executable
    if not ffmpeg_executable:
        base_params["merge_output_format"] = None
    if merge_output_format and ffmpeg_executable:
        base_params["merge_output_format"] = merge_output_format

    # Anonymous default first; on transient blocks (403/CRT wall) retry with
    # captured request cookies and, for YouTube only, browser-profile cookies.
    # Each attempt re-extracts fresh signed URLs and retries are bounded.
    info = None
    browser_name = _detect_browser() if _is_youtube_url(url) else None
    last_error = "yt-dlp returned no info"
    for index, (client, attempt_cookies) in enumerate(
        _youtube_attempts(
            url=url,
            cookies=cookies,
            browser_name=browser_name,
            for_download=True,
        )
    ):
        delay = _backoff_before_attempt(index)
        if delay:
            logger.info("Waiting %.0fs before retrying %s", delay, url)
            time.sleep(delay)
        use_cookies = attempt_cookies if attempt_cookies != "_browser_" else None
        cookiefile = _write_cookiefile(url, use_cookies)
        params = copy.deepcopy(base_params)
        if client:
            _set_player_client(params, client)
        if cookiefile is not None:
            params["cookiefile"] = str(cookiefile)
        elif attempt_cookies == "_browser_" and browser_name:
            params["cookiesfrombrowser"] = (browser_name,)
            logger.info(
                "Using browser cookie jar (%s) for %s", browser_name, url
            )
        try:
            try:
                params.pop("_magnetoclip_url", None)
                with YoutubeDL(params) as ydl:
                    info = ydl.extract_info(url, download=True)
            except DownloadCancelled:
                raise
            except Exception as exc:
                message = str(exc)
                if attempt_cookies == "_browser_":
                    # Reading Chrome's app-bound-encrypted cookies on Windows
                    # can fail (Chrome v127+ while the browser is running); it is
                    # an environment limitation, not a media error, so skip to
                    # the next attempt instead of aborting the whole chain.
                    logger.warning(
                        "Browser cookie attempt failed for %s: %s", url, message
                    )
                    # Browser cookie extraction is optional. Preserve the
                    # actual media/extractor error rather than surfacing a
                    # Chrome database-copy/lock failure to the user.
                    continue
                if not _is_stream_retryable_error(message):
                    logger.warning("Stream download failed for %s: %s", url, message)
                    raise
                last_error = message
                logger.warning(
                    "Retrying stream download for %s (client=%s): %s", url, client, message
                )
                continue
        finally:
            if cookiefile is not None:
                try:
                    cookiefile.unlink(missing_ok=True)
                except OSError:
                    pass
        break

    if not info:
        raise StreamResolutionError(last_error)

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else info

    # Top-level filepath is yt-dlp's postprocessed/merged file. The first
    # requested_downloads filepath can point at the intermediate video-only
    # stream, so do not prefer it over the merged output.
    final_path = info.get("filepath") or info.get("_filename")
    requested = (info.get("requested_downloads") or [])
    if not final_path and requested:
        paths = [item.get("filepath") for item in requested if item.get("filepath")]
        final_path = paths[-1] if paths else None
    if not final_path:
        final_path = ydl.prepare_filename(info)
    if not final_path:
        candidates = list(save_dir.iterdir())
        if candidates:
            final_path = str(candidates[-1])

    return Path(final_path)
