"""HLS playlist parsing (master/variant/media, EXT-X-MEDIA renditions).

Split out of :mod:`magnetoclip.media.browser_streaming` so the parser is usable
by the analyzer and download engine without pulling the browser transport in.
The downloader re-exports these symbols, so existing imports keep working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

# EXT-X-KEY ``METHOD`` values the downloader can decrypt. Everything else
# (``SAMPLE-AES`` variants) requires DRM we cannot reproduce.
SUPPORTED_KEY_METHODS = {"AES-128", "NONE"}

_ATTR_RE = re.compile(r"([A-Z0-9-]+)=(?:\"([^\"]*)\"|([^,\s]+))")


@dataclass
class HlsSegment:
    url: str
    duration: float = 0.0
    key_uri: str | None = None
    key_method: str = "NONE"
    iv_hex: str | None = None
    init_uri: str | None = None


@dataclass
class HlsVariant:
    url: str
    bandwidth: int = 0
    height: int | None = None
    codecs: str = ""
    audio_group: str | None = None
    audio_url: str | None = None


@dataclass
class HlsMediaRendition:
    """An ``EXT-X-MEDIA`` alternate rendition (e.g. a separate audio track)."""

    group_id: str | None
    name: str
    url: str
    language: str | None = None
    default: bool = False
    autoselect: bool = False


@dataclass
class HlsPlaylist:
    is_master: bool
    variants: list[HlsVariant] = field(default_factory=list)
    segments: list[HlsSegment] = field(default_factory=list)
    media_sequence: int = 0
    init_uri: str | None = None
    total_duration: float = 0.0
    bandwidth: int = 0
    audio_only: bool = False
    renditions: list[HlsMediaRendition] = field(default_factory=list)


@dataclass
class _KeyState:
    uri: str | None = None
    method: str = ""
    iv_hex: str | None = None


def _resolve_hls_url(base_url: str, ref: str) -> str:
    """Resolve an HLS child URL (variant/segment/key/init) against *base_url*,
    preserving the base URL's query string for same-origin references.

    Tokenized CDNs (anime/movie hosts like nexabloom, megaplay, ...) sign the
    master playlist URL with ``?token=...`` and every child playlist, key and
    segment request must carry it. ``urljoin`` drops the base query when *ref*
    is relative, so combining the two yields an untokenized URL that the CDN
    rejects with 403. We therefore append the base query whenever the child
    URL lives on the same host as *base_url* and would otherwise lose it.
    Cross-origin absolute references keep their own (already signed) query.
    """
    if not ref:
        return base_url
    if ref.startswith(("http://", "https://", "//")):
        joined = urljoin(base_url, ref)
    else:
        joined = urljoin(base_url, ref)
        if "?" not in joined and "?" in base_url:
            return f"{joined}?{base_url.split('?', 1)[1]}"
        return joined
    if "?" not in joined and "?" in base_url:
        base_host = urlparse(base_url).netloc.lower()
        ref_host = urlparse(ref).netloc.lower()
        if base_host and base_host == ref_host:
            return f"{joined}?{base_url.split('?', 1)[1]}"
    return joined


def parse_hls(text: str, base_url: str) -> HlsPlaylist:
    """Parse an HLS playlist (master or media) into a :class:`HlsPlaylist`."""
    is_master = "#EXT-X-STREAM-INF" in text
    variants: list[HlsVariant] = []
    segments: list[HlsSegment] = []
    media_sequence = 0
    renditions: list[HlsMediaRendition] = []
    rendition_by_group: dict[str, HlsMediaRendition] = {}

    key: _KeyState = _KeyState()
    pending_variant_attrs: dict[str, str] | None = None
    extinf_duration = 0.0
    init_uri: str | None = None
    total_duration = 0.0
    bandwidth = 0
    audio_only = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#EXTM3U", "#EXT-X-VERSION")):
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = _parse_attrs(line)
            pending_variant_attrs = attrs
            if not is_master:
                is_master = True
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            seq = line.split(":", 1)[-1].strip()
            media_sequence = int(seq) if seq.isdigit() else 0
            continue
        if line.startswith("#EXT-X-KEY"):
            attrs = _parse_attrs(line)
            key.method = attrs.get("METHOD", "")
            key.uri = attrs.get("URI")
            key.iv_hex = attrs.get("IV")
            continue
        if line.startswith("#EXT-X-MAP"):
            attrs = _parse_attrs(line)
            init_uri = attrs.get("URI")
            continue
        if line.startswith("#EXTINF"):
            duration = line.split(":", 1)[-1].split(",", 1)[0].strip()
            try:
                extinf_duration = float(duration)
            except ValueError:
                extinf_duration = 0.0
            continue
        if line.startswith("#EXT-X-AUDIO"):
            continue
        if line.startswith("#EXT-X-MEDIA"):
            attrs = _parse_attrs(line)
            if attrs.get("TYPE", "").upper() == "AUDIO" and attrs.get("URI"):
                uri = attrs["URI"]
                rendition = HlsMediaRendition(
                    group_id=attrs.get("GROUP-ID"),
                    name=attrs.get("NAME") or "",
                    url=_resolve_hls_url(base_url, uri),
                    language=attrs.get("LANGUAGE"),
                    default=_yes(attrs.get("DEFAULT")),
                    autoselect=_yes(attrs.get("AUTOSELECT")),
                )
                renditions.append(rendition)
                if rendition.group_id and rendition.group_id not in rendition_by_group:
                    rendition_by_group[rendition.group_id] = rendition
            continue
        if line.startswith("#"):
            continue
        uri = line
        if uri.startswith('"') and uri.endswith('"'):
            uri = uri[1:-1]
        target = _resolve_hls_url(base_url, uri)
        if pending_variant_attrs is not None:
            audio_group = pending_variant_attrs.get("AUDIO")
            variants.append(
                HlsVariant(
                    url=target,
                    bandwidth=_int_attr(pending_variant_attrs, "BANDWIDTH"),
                    height=_resolution_height(pending_variant_attrs.get("RESOLUTION")),
                    codecs=pending_variant_attrs.get("CODECS", ""),
                    audio_group=audio_group,
                    audio_url=(
                        rendition_by_group[audio_group].url
                        if audio_group in rendition_by_group
                        else None
                    ),
                )
            )
            bandwidth = max(bandwidth, _int_attr(pending_variant_attrs, "BANDWIDTH"))
            pending_variant_attrs = None
            continue
        segments.append(
            HlsSegment(
                url=target,
                duration=extinf_duration,
                key_uri=_resolve_hls_url(base_url, key.uri) if key.uri else None,
                iv_hex=key.iv_hex,
                init_uri=_resolve_hls_url(base_url, init_uri) if init_uri else None,
            )
        )
        total_duration += extinf_duration
        extinf_duration = 0.0

    if is_master:
        # EXT-X-MEDIA is allowed to appear after EXT-X-STREAM-INF. Bind audio
        # groups in a second pass so playlist ordering does not lose the
        # separate audio rendition.
        for variant in variants:
            if variant.audio_group and not variant.audio_url:
                rendition = rendition_by_group.get(variant.audio_group)
                if rendition is not None:
                    variant.audio_url = rendition.url
        return HlsPlaylist(
            is_master=True,
            variants=variants,
            bandwidth=bandwidth,
            audio_only=audio_only,
            renditions=renditions,
        )
    return HlsPlaylist(
        is_master=False,
        segments=segments,
        media_sequence=media_sequence,
        init_uri=_resolve_hls_url(base_url, init_uri) if init_uri else None,
        total_duration=total_duration,
        bandwidth=bandwidth,
        audio_only=audio_only,
        renditions=renditions,
    )


def _yes(value: str | None) -> bool:
    return (value or "").upper() in {"YES", "TRUE", "1"}


def _parse_attrs(line: str) -> dict[str, str]:
    pos = line.find(":")
    return {
        m.group(1): (m.group(2) if m.group(2) is not None else m.group(3))
        for m in _ATTR_RE.finditer(line[pos + 1 :] if pos >= 0 else line)
    }


def _int_attr(attrs: dict[str, str], name: str) -> int:
    value = attrs.get(name) or "0"
    try:
        return int(value)
    except ValueError:
        return 0


def _resolution_height(resolution: str | None) -> int | None:
    if not resolution:
        return None
    match = re.match(r"(\d+)x(\d+)", resolution.strip())
    return int(match.group(2)) if match else None


def select_variant(variants: list[HlsVariant], quality: str) -> HlsVariant:
    """Select the highest available variant at or below the requested height.

    ``quality`` accepts ``best``/``audio`` and common labels such as ``360``,
    ``360p``, ``720``, ``1080p``, ``1440p`` and ``2160p``. If no variant is at
    or below the requested height, the smallest available variant is used; the
    engine never silently upgrades a requested cap.
    """
    if not variants:
        raise ValueError("the master playlist has no video variants")
    normalized = str(quality or "best").strip().lower().replace(" ", "")
    if normalized in {"best", "highest", "max", "auto"}:
        return max(variants, key=lambda v: (v.height or 0, v.bandwidth))
    if normalized in {"audio", "audio-only", "audioonly"}:
        raise ValueError("audio quality is not a video variant")
    match = re.search(r"(\d{3,4})", normalized)
    if not match:
        return max(variants, key=lambda v: (v.height or 0, v.bandwidth))
    target = int(match.group(1))
    capped = [v for v in variants if v.height is not None and v.height <= target]
    if capped:
        return max(capped, key=lambda v: (v.height or 0, v.bandwidth))
    known = [v for v in variants if v.height is not None]
    return min(known or variants, key=lambda v: (v.height or 0, v.bandwidth))


def total_media_duration(segments: list[HlsSegment]) -> float:
    """Sum of segment durations for a parsed media playlist."""
    return sum(float(seg.duration or 0.0) for seg in segments)