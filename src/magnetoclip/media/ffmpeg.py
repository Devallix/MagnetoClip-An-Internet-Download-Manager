"""Optional ffmpeg/ffprobe bridge for deep media metadata."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from magnetoclip.services.logging import get_logger

log = get_logger(__name__)

DEFAULT_FORMAT = "json"


class FFmpegNotFoundError(RuntimeError):
    pass


class FFmpegLocator:
    """Finds ffprobe/ffmpeg on the system (or from an explicit override)."""

    def __init__(
        self,
        ffprobe_path: str | Path | None = None,
        ffmpeg_path: str | Path | None = None,
    ) -> None:
        self.ffprobe_path = self._resolve(ffprobe_path, "ffprobe")
        self.ffmpeg_path = self._resolve(ffmpeg_path, "ffmpeg")

    @staticmethod
    def _resolve(explicit: str | Path | None, name: str) -> str | None:
        if explicit:
            return str(explicit)
        return shutil.which(name)

    @property
    def available(self) -> bool:
        return self.ffprobe_path is not None

    def probe_path(self) -> str:
        if self.ffprobe_path is None:
            raise FFmpegNotFoundError("ffprobe is not available")
        return self.ffprobe_path


async def probe(path: str | Path, locator: FFmpegLocator | None = None) -> dict | None:
    """Probe a media file with ffprobe; returns None when unavailable/fails."""
    locator = locator or FFmpegLocator()
    if not locator.available:
        return None
    command = [
        locator.probe_path(),
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", DEFAULT_FORMAT,
        str(path),
    ]
    try:
        result = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(result.communicate(), timeout=30)
        if result.returncode != 0:
            log.warning("ffprobe_nonzero", path=str(path), code=result.returncode)
            return None
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("ffprobe_failed", path=str(path), error=str(exc))
        return None
    try:
        return _summarize(json.loads(stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("ffprobe_bad_json", path=str(path), error=str(exc))
        return None


def _summarize(raw: dict) -> dict:
    format_info = raw.get("format", {})
    video_stream = _first_stream(raw, "video")
    audio_stream = _first_stream(raw, "audio")
    summary: dict = {
        "format_name": format_info.get("format_name"),
        "duration": _safe_float(format_info.get("duration")),
        "bit_rate": _safe_int(format_info.get("bit_rate")),
        "size": _safe_int(format_info.get("size")),
    }
    if video_stream:
        summary["codec"] = video_stream.get("codec_name")
        summary["width"] = _safe_int(video_stream.get("width"))
        summary["height"] = _safe_int(video_stream.get("height"))
        summary["fps"] = _safe_float(video_stream.get("avg_frame_rate")) or _safe_float(
            video_stream.get("r_frame_rate")
        )
    if audio_stream:
        summary["audio_codec"] = audio_stream.get("codec_name")
        summary["channels"] = _safe_int(audio_stream.get("channels"))
        summary["sample_rate"] = _safe_int(audio_stream.get("sample_rate"))
    return {k: v for k, v in summary.items() if v is not None}


def _first_stream(raw: dict, kind: str) -> dict | None:
    for stream in raw.get("streams", []):
        if stream.get("codec_type") == kind:
            return stream
    return None


def _safe_float(value: object) -> float | None:
    if value is None or value in ("", "N/A"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed else parsed  # drop NaN


def _safe_int(value: object) -> int | None:
    if value is None or value in ("", "N/A"):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return None if parsed == 0 else parsed  # 0 typically means "unknown"
