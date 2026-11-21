"""Helpers for presenting per-stream resolution choices.

This module deliberately contains no Qt imports so resolution option building
can be tested independently from the desktop UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class StreamQualityOption:
    quality: str
    label: str
    detail: str = ""
    height: int | None = None
    bandwidth: int = 0
    requires_merge: bool = False


@dataclass(frozen=True)
class StreamQualityProbe:
    title: str
    options: tuple[StreamQualityOption, ...]
    note: str = ""
    source: str = "unknown"


def _value(item: Any, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _height(item: Any) -> int | None:
    raw = _value(item, "height")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 120 <= value <= 8640 else None


def _bandwidth(item: Any) -> int:
    for key in ("bandwidth", "tbr", "vbr"):
        try:
            value = int(float(_value(item, key, 0) or 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _is_video(item: Any) -> bool:
    audio_only = bool(_value(item, "audio_only", False))
    if audio_only:
        return False
    vcodec = _value(item, "vcodec")
    if vcodec is not None and str(vcodec).lower() == "none":
        return False
    has_video = _value(item, "has_video")
    if has_video is False:
        return False
    return True


def build_quality_options(
    formats: Iterable[Any] | None,
    *,
    ffmpeg_available: bool = True,
    manifest_kind: str = "",
) -> tuple[StreamQualityOption, ...]:
    """Build a stable ``best`` + descending-height option list.

    yt-dlp dictionaries and MagnetoClip ``MediaFormat`` objects are both
    accepted. The always-present ``best`` option is intentional: a metadata
    probe that exposes no resolution ladder must never prevent downloading.
    """
    kind = str(manifest_kind or "").strip().lower()
    rows = [item for item in (formats or []) if _is_video(item)]

    # Without FFmpeg, hide HLS variants that are known to require a separate
    # alternate-audio rendition. The best-available fallback remains available
    # so the resolver can choose a single-file rendition if the provider has one.
    if kind == "hls" and not ffmpeg_available:
        safe_rows = []
        for item in rows:
            acodec = _value(item, "acodec")
            vcodec = _value(item, "vcodec")
            needs_separate_audio = (
                acodec is not None
                and str(acodec).lower() == "none"
                and vcodec is not None
                and str(vcodec).lower() != "none"
            )
            if not _value(item, "audio_url") and not needs_separate_audio:
                safe_rows.append(item)
        rows = safe_rows

    by_height: dict[int, Any] = {}
    for item in rows:
        height = _height(item)
        if height is None:
            continue
        current = by_height.get(height)
        if current is None or _bandwidth(item) > _bandwidth(current):
            by_height[height] = item

    options: list[StreamQualityOption] = [
        StreamQualityOption(
            quality="best",
            label="Best available",
            detail="Automatically select the best compatible video and audio streams",
        )
    ]
    for height in sorted(by_height, reverse=True):
        item = by_height[height]
        requires_merge = bool(
            _value(item, "audio_url")
            or (
                _value(item, "acodec") in (None, "none")
                and _value(item, "vcodec") not in (None, "none")
                and ffmpeg_available
            )
        )
        bandwidth = _bandwidth(item)
        detail_parts = []
        if bandwidth:
            protocol = str(_value(item, "protocol", "") or "").lower()
            mbps = bandwidth / 1_000_000 if protocol == "hls" or bandwidth > 100_000 else bandwidth / 1000
            detail_parts.append(f"{mbps:.1f} Mb/s")
        if requires_merge:
            detail_parts.append("video + audio merge")
        elif _value(item, "acodec") not in (None, "none") or _value(item, "audio_url") is None:
            detail_parts.append("audio included when provided by source")
        options.append(
            StreamQualityOption(
                quality=str(height),
                label=f"{height}p",
                detail=" · ".join(detail_parts) or "Available source resolution",
                height=height,
                bandwidth=bandwidth,
                requires_merge=requires_merge,
            )
        )
    return tuple(options)


def best_only_probe(
    *, title: str = "Streaming video", note: str = "", source: str = "fallback"
) -> StreamQualityProbe:
    return StreamQualityProbe(
        title=title,
        options=build_quality_options([]),
        note=note,
        source=source,
    )


__all__ = [
    "StreamQualityOption",
    "StreamQualityProbe",
    "build_quality_options",
    "best_only_probe",
]
