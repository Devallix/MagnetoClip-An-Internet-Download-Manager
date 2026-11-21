"""Shared YouTube download-flow helpers for UI entry points.

The resolution dialog is Qt/UI-only and cannot run inside the browser native
messaging host. Every path that funnels a YouTube video into ``manager.add``
(manual URL dialog, browser captures, the Detected page) goes through the same
helper here so users always get the "pick a resolution" step instead of
silently using the global ``streaming.quality`` default.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog


def is_youtube_video_url(url: str) -> bool:
    """Return True when *url* is a YouTube video (or a video inside a playlist)."""
    from magnetoclip.media.youtube.detector import YouTubeURLDetector
    from magnetoclip.media.youtube.types import YouTubeURLType

    try:
        return YouTubeURLDetector.classify(url) in (
            YouTubeURLType.video,
            YouTubeURLType.video_with_playlist,
        )
    except Exception:  # noqa: BLE001 - a bad URL simply is not a YouTube video
        return False


def youtube_quality_kwargs(
    context, url: str, *, cookies: dict[str, str] | None = None, parent=None
) -> dict | None:
    """Compute per-download YouTube quality kwargs for ``manager.add``.

    Returns:
        ``None`` when the user cancelled the quality dialog (the caller should
        abort the add). ``{}`` when no quality step applies (non-YouTube, or a
        concrete ``youtube.default_quality`` from settings that needs no
        dialog). Otherwise the ``youtube_*`` kwargs recorded from the dialog.
    """
    if not is_youtube_video_url(url):
        return {}
    settings = getattr(context, "settings", None)
    default_quality = "ask"
    if settings is not None:
        default_quality = str(settings.get("youtube.default_quality", "ask") or "ask")
    if default_quality != "ask":
        return {
            "youtube_quality": default_quality,
            "youtube_requested_quality": default_quality,
        }
    from magnetoclip.ui.dialogs.youtube_quality import YouTubeQualityDialog

    manager = getattr(context, "manager", None)
    on_ffmpeg_located = None
    manager_settings = getattr(manager, "settings", None)
    if manager_settings is not None:
        on_ffmpeg_located = lambda path: manager_settings.set(
            "streaming.ffmpeg_path", path
        )

    quality_dialog = YouTubeQualityDialog(
        url,
        context=context,
        cookies=cookies,
        ffmpeg_available=None,
        on_ffmpeg_located=on_ffmpeg_located,
        parent=parent,
    )
    if quality_dialog.exec() != QDialog.Accepted:
        return None
    result = quality_dialog.result
    if result is None:
        return {}
    record = result.to_record()
    quality_key = "best" if not record.get("height") else str(record["height"])
    return {
        "youtube_quality": quality_key,
        "youtube_height": record.get("height"),
        "youtube_format": record.get("format_id"),
        "youtube_format_selector": record.get("format_selector"),
        "youtube_merge_output_format": record.get("merge_output_format"),
        "youtube_format_label": record.get("format_label"),
        "youtube_requested_quality": record.get("requested_quality"),
    }