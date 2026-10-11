"""HLS stream analysis: resolve a manifest URL into a :class:`MediaInfo`.

The analyzer is resolve-only: it pulls the master (and, for duration/size
hints, the best variant) playlist and turns the parsed variants + audio
renditions into :class:`~magnetoclip.media.models.MediaFormat` entries, without
downloading any segments. Fetching goes through the injected transport so both
the browser (Cloudflare-clearing) and direct HTTP paths can back it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import unquote, urlparse

from ..models import MediaFormat, MediaInfo, RequestContext
from .parser import HlsPlaylist, HlsVariant, parse_hls

_TITLE_SANITIZE_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")

MODULE_GENERIC_SLUGS = {
    "index",
    "master",
    "playlist",
    "stream",
    "index-master",
    "m3u8",
    "watch",
    "embed",
    "e",
    "v",
    "video",
}


def title_from_page(referer_page: str | None, fallback: str = "stream") -> str:
    """Derive a human title from the captured page/player URL."""
    if not referer_page:
        return fallback
    page = referer_page.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    last = unquote(page.rsplit("/", 1)[-1]) if page else ""
    if not last or last in MODULE_GENERIC_SLUGS or last.startswith("."):
        return fallback
    cleaned = _TITLE_SANITIZE_RE.sub("_", last).strip()
    return cleaned[:100] or fallback


def analyze_master(
    url: str,
    master_text: str,
    *,
    fetch_playlist: Callable[[str], str] | None = None,
    title: str = "stream",
) -> MediaInfo:
    """Build a :class:`MediaInfo` from a master playlist text.

    *fetch_playlist* is consulted (when the master references variants) to read
    a media playlist for the top variant, giving a duration/size estimate.
    """
    if "#EXT" not in master_text:
        raise ValueError("not an HLS playlist (no #EXT tags in the response)")
    master = parse_hls(master_text, url)
    if not master.is_master:
        # A direct media playlist: single stream, no quality ladder.
        media = _media_format("init", master, url)
        return MediaInfo(
            url=url,
            title=title,
            ext="mp4",
            media_type="audio" if master.audio_only else "video",
            duration=master.total_duration or None,
            formats=[media],
        )

    formats: list[MediaFormat] = []
    duration: float | None = None
    best_variant = _pick_best_variant(master)
    media_playlist: HlsPlaylist | None = None
    if best_variant is not None and fetch_playlist is not None:
        try:
            text = fetch_playlist(best_variant.url)
            if "#EXT" in text:
                media_playlist = parse_hls(text, best_variant.url)
                if not media_playlist.is_master and media_playlist.total_duration > 0:
                    duration = media_playlist.total_duration
        except Exception:  # noqa: BLE001 - a duration estimate must never fail analysis
            media_playlist = None

    for variant in sorted(master.variants, key=lambda v: (v.height or 0, v.bandwidth)):
        formats.append(_variant_format(variant, duration))

    audio_formats = _audio_formats(master, duration, seen_variant_audio=True)
    formats.extend(audio_formats)

    audio_only = not any(not f.audio_only for f in formats)
    media_type = "audio" if audio_only else "video"
    return MediaInfo(
        url=url,
        title=title,
        ext="m4a" if audio_only else "mp4",
        media_type=media_type,
        duration=duration,
        formats=formats,
    )


def resolve_hls_media(
    url: str,
    *,
    session_factory=None,
    context: RequestContext | None = None,
    fetch: Callable[[str], str] | None = None,
    title: str = "stream",
) -> MediaInfo:
    """Fetch and analyze an HLS manifest URL.

    Transport priority: an explicit *fetch* callable, else the browser
    extension (via *session_factory* + *context*.referer), which clears
    Cloudflare challenges on streaming-site CDNs.
    """
    if fetch is None:
        if session_factory is None:
            raise ValueError("resolve_hls_media needs fetch or session_factory")
        from ._browser_fetch import browser_fetch_text  # local to avoid cycles

        fetch = lambda manifest_url: browser_fetch_text(
            session_factory, manifest_url, referer=(context.referer if context else None)
        )
    master_text = fetch(url)
    info = analyze_master(
        url,
        master_text,
        fetch_playlist=fetch,
        title=title,
    )
    info.context = context
    return info


def _media_format(format_id: str, playlist: HlsPlaylist, base_url: str) -> MediaFormat:
    duration = playlist.total_duration or 0.0
    return MediaFormat(
        format_id=format_id,
        ext="m4a" if playlist.audio_only else "mp4",
        protocol="hls",
        bandwidth=playlist.bandwidth,
        audio_only=playlist.audio_only or not playlist.segments,
        has_video=not playlist.audio_only,
        video_url=playlist.segments[0].url if playlist.segments else base_url,
        filesize_est=_estimate_size(playlist.bandwidth, duration),
    )


def _pick_best_variant(master: HlsPlaylist) -> HlsVariant | None:
    if not master.variants:
        return None
    return max(master.variants, key=lambda v: (v.height or 0, v.bandwidth))


def _variant_format(variant: HlsVariant, duration: float | None) -> MediaFormat:
    bandwidth = variant.bandwidth
    est = _estimate_size(bandwidth, duration) if duration else None
    return MediaFormat(
        format_id=f"hls-{variant.height or 0}-{bandwidth or 0}",
        ext="mp4",
        height=variant.height,
        bandwidth=bandwidth,
        protocol="hls",
        video_url=variant.url,
        audio_url=variant.audio_url,
        audio_only=False,
        has_video=True,
        filesize_est=est,
    )


def _audio_formats(
    master: HlsPlaylist, duration: float | None, *, seen_variant_audio: bool = False
) -> list[MediaFormat]:
    """Standalone formats for alternate audio renditions.

    Variants already carry their matched rendition as ``audio_url``; when a
    rendition group matches a variant that URL is handled there. Any rendition
    that is not consumed by a video variant becomes its own audio-only format.
    """
    used_groups = {v.audio_group for v in master.variants if v.audio_group}
    formats: list[MediaFormat] = []
    for index, rendition in enumerate(master.renditions):
        if seen_variant_audio and rendition.group_id in used_groups:
            continue
        formats.append(
            MediaFormat(
                format_id=f"hls-audio-{sanitize_id(rendition.name) or index}",
                ext="m4a",
                bandwidth=0,
                protocol="hls",
                audio_url=rendition.url,
                audio_only=True,
                has_video=False,
                filesize_est=_estimate_size(0, duration) if duration else None,
            )
        )
    return formats


def _estimate_size(bandwidth: int, duration: float | None) -> int | None:
    if bandwidth <= 0 or not duration or duration <= 0:
        return None
    return int(bandwidth * duration / 8)


def sanitize_id(name: str) -> str:
    cleaned = re.sub(r"[\W_]+", "-", name).strip("-")
    return cleaned[:24].lower()


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""