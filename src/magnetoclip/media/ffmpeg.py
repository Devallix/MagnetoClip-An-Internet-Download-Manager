"""Optional ffmpeg/ffprobe bridge for deep media metadata and stream assembly.

Beyond metadata probing this is the app's FFmpeg *manager*: it resolves the
binary (user-configured path -> bundled -> system ``PATH``) and exposes async
``remux``/``merge`` helpers with progress and cancellation, used by the HLS
engine to turn transport streams into MP4 and to mux separate audio renditions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from magnetoclip.services.logging import get_logger

from .streaming import DownloadCancelled

log = get_logger(__name__)

DEFAULT_FORMAT = "json"


def _windowless_kwargs() -> dict:
    """Kwargs that run a spawned binary without a console window.

    On Windows every console executable spawned by the app - ffmpeg, ffprobe -
    opens a command-prompt window for as long as it runs unless told otherwise.
    A preview transcode takes seconds, so the first preview of a file would
    leave a stray ``cmd`` window on the user's screen. ``CREATE_NO_WINDOW``
    keeps the process headless; on non-Windows nothing is needed.
    """
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


class FFmpegNotFoundError(RuntimeError):
    pass


class FFmpegRunError(RuntimeError):
    """An ffmpeg invocation failed to run or exited non-zero."""


def bundled_bin_dir() -> Path:
    """Directory holding any binary shipped with the app (``resources/bin``).

    The app ships an LGPL ffmpeg so previews work on machines that have no
    ffmpeg of their own. In a frozen build the modules live under ``_MEIPASS``;
    from source they sit at ``src/magnetoclip/media/ffmpeg.py``.
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "magnetoclip" / "resources" / "bin")
    candidates.append(Path(__file__).resolve().parents[1] / "resources" / "bin")
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return candidates[-1]


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


class FFmpegLocator:
    """Finds ffprobe/ffmpeg: configured path -> bundled -> system PATH."""

    def __init__(
        self,
        ffprobe_path: str | Path | None = None,
        ffmpeg_path: str | Path | None = None,
        settings=None,
    ) -> None:
        self.settings = settings
        custom_root = self._custom_root()
        self.ffprobe_path = self._resolve(ffprobe_path, "ffprobe", custom_root)
        self.ffmpeg_path = self._resolve(ffmpeg_path, "ffmpeg", custom_root)

    def _custom_root(self) -> Path | None:
        if self.settings is None:
            return None
        raw = str(self.settings.get("streaming.ffmpeg_path", "") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path.parent if path.is_file() else path

    @classmethod
    def _resolve(
        cls, explicit: str | Path | None, name: str, custom_root: Path | None
    ) -> str | None:
        if explicit:
            return str(explicit)
        candidates: list[Path] = []
        if custom_root is not None:
            candidates += [custom_root / _exe(name), custom_root / name]
        bundled = bundled_bin_dir()
        candidates += [bundled / _exe(name), bundled / name]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        return shutil.which(name)

    @property
    def available(self) -> bool:
        return self.ffprobe_path is not None

    @property
    def ffmpeg_available(self) -> bool:
        return self.ffmpeg_path is not None

    def probe_path(self) -> str:
        if self.ffprobe_path is None:
            raise FFmpegNotFoundError("ffprobe is not available")
        return self.ffprobe_path

    def ffmpeg(self) -> str:
        if self.ffmpeg_path is None:
            raise FFmpegNotFoundError("ffmpeg is not available")
        return self.ffmpeg_path


def build_remux_command(
    ffmpeg: str, src: str | Path, dst: str | Path, *, extra_args: list[str] | None = None
) -> list[str]:
    """Copy-remux *src* into *dst* without re-encoding."""
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-i", str(src),
        "-c", "copy",
        *(extra_args or []),
        str(dst),
    ]


# Video encoders to try for a preview proxy, best first.
#
# libx264 is excluded from the bundled build on purpose: it is GPL, and shipping
# it inside this proprietary app would impose GPL on the whole distribution.
# The LGPL build instead encodes H.264 with libopenh264 (BSD), and every build
# has mpeg4 as the last resort. Selecting by what the binary actually supports
# keeps a system ffmpeg (often the GPL one) on the better encoder.
PREVIEW_ENCODERS: tuple[str, ...] = ("libx264", "libopenh264", "mpeg4")
_PREVIEW_ENCODER_ARGS: dict[str, list[str]] = {
    "libx264": ["-preset", "veryfast", "-crf", "30"],
    "libopenh264": ["-b:v", "1200k", "-maxrate", "1400k", "-bufsize", "2400k"],
    "mpeg4": ["-q:v", "6"],
}


def _supported_encoders(ffmpeg: str) -> set[str] | None:
    """Encoders *ffmpeg* reports, or None when the list cannot be read.

    Result is cached per binary path: this runs once per preview proxy, and
    probing costs a process launch.
    """
    cached = _ENCODER_CACHE.get(ffmpeg)
    if cached is not None:
        return cached or None
    names: set[str] = set()
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
            **_windowless_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        _ENCODER_CACHE[ffmpeg] = set()
        return None
    for line in result.stdout.decode(errors="replace").splitlines():
        # Lines look like " V....D libx264   H.264 ...".
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    _ENCODER_CACHE[ffmpeg] = names
    return names or None


def preview_encoders(ffmpeg: str) -> tuple[str, ...]:
    """The preview encoders to try against this *ffmpeg*, best first.

    Falls back to every known encoder when the binary's list is unreadable, so
    an unusual build still gets a chance rather than silently losing previews.
    """
    supported = _supported_encoders(ffmpeg)
    if supported is None:
        return PREVIEW_ENCODERS
    usable = tuple(name for name in PREVIEW_ENCODERS if name in supported)
    return usable or ("mpeg4",)


_ENCODER_CACHE: dict[str, set[str]] = {}


def build_preview_proxy_command(
    ffmpeg: str,
    src: str | Path,
    dst: str | Path,
    *,
    video_encoder: str = "libx264",
    max_seconds: int | None = None,
    max_width: int = 640,
) -> list[str]:
    """Re-encode *src* into a small, widely decodable preview proxy.

    Qt's bundled FFmpeg has no software AV1 decoder, so an AV1 file decodes to
    nothing and the preview would be a silent black rectangle. Re-encoding to
    H.264 (falling back to MPEG-4) produces something the same build can play.
    Audio is stream-copied, so this is video-only work.
    """
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-i", str(src),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-sn",
        "-dn",
        "-vf", f"scale=min({max_width}\\,iw):-2",
        "-c:v", video_encoder,
        *_PREVIEW_ENCODER_ARGS.get(video_encoder, []),
        "-c:a", "copy",
        *(["-t", str(max_seconds)] if max_seconds else []),
        str(dst),
    ]


def build_merge_command(
    ffmpeg: str,
    video: str | Path,
    audio: str | Path,
    dst: str | Path,
    *,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Mux a video-only stream with an audio rendition (copy, no re-encode)."""
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c", "copy",
        "-shortest",
        *(extra_args or []),
        str(dst),
    ]


def _parse_progress_line(line: bytes) -> float | None:
    """Return elapsed seconds from an ffmpeg ``-progress`` key/value line."""
    try:
        key, _, value = line.decode(errors="replace").strip().partition("=")
    except Exception:  # noqa: BLE001 - unparsable progress is simply skipped
        return None
    if key in ("out_time_us", "out_time_ms"):
        try:
            return max(0.0, int(value) / 1_000_000)
        except ValueError:
            return None
    return None


async def _drain_stream(stream, sink: list[bytes]) -> None:
    while True:
        chunk = await stream.readline()
        if not chunk:
            return
        sink.append(chunk)


_create_subprocess = asyncio.create_subprocess_exec


async def _run_ffmpeg(
    command: list[str],
    *,
    progress_cb=None,
    cancel_event=None,
    total_duration: float | None = None,
) -> None:
    try:
        process = await _create_subprocess(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_windowless_kwargs(),
        )
    except OSError as exc:
        raise FFmpegRunError(f"could not start ffmpeg: {exc}")

    stderr_lines: list[bytes] = []
    drain = asyncio.create_task(_drain_stream(process.stderr, stderr_lines))
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
                raise DownloadCancelled()
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=0.2)
            except TimeoutError:
                if process.returncode is not None:
                    break
                continue
            if not line:
                break
            seconds = _parse_progress_line(line)
            if seconds is not None and progress_cb is not None:
                progress_cb(seconds, total_duration)
    finally:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(drain, timeout=1.0)
        if process.returncode is None:
            await process.wait()

    if process.returncode != 0:
        stderr = b"".join(stderr_lines).decode(errors="replace").strip()
        raise FFmpegRunError(stderr[:400] or f"ffmpeg exited with code {process.returncode}")


async def remux(
    src: str | Path,
    dst: str | Path,
    *,
    locator: FFmpegLocator | None = None,
    progress_cb=None,
    cancel_event=None,
    total_duration: float | None = None,
) -> Path:
    """Copy-remux *src* to *dst* (no re-encode); returns the output path."""
    ffmpeg = (locator or FFmpegLocator()).ffmpeg()
    destination = Path(dst)
    await _run_ffmpeg(
        build_remux_command(ffmpeg, src, destination),
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        total_duration=total_duration,
    )
    if not destination.exists():
        raise FFmpegRunError("ffmpeg did not produce an output file")
    return destination


async def merge(
    video: str | Path,
    audio: str | Path,
    dst: str | Path,
    *,
    locator: FFmpegLocator | None = None,
    progress_cb=None,
    cancel_event=None,
    total_duration: float | None = None,
) -> Path:
    """Mux a video stream with a separate audio rendition; returns *dst*."""
    ffmpeg = (locator or FFmpegLocator()).ffmpeg()
    destination = Path(dst)
    await _run_ffmpeg(
        build_merge_command(ffmpeg, video, audio, destination),
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        total_duration=total_duration,
    )
    if not destination.exists():
        raise FFmpegRunError("ffmpeg did not produce an output file")
    return destination


def _run_ffmpeg_sync(
    command: list[str],
    *,
    progress_cb=None,
    cancel_event=None,
) -> tuple[int, str]:
    """Run *command*, returning ``(exit_code, stderr_tail)``.

    The blocking counterpart of :func:`_run_ffmpeg`, for callers living on a
    worker thread rather than the asyncio loop.
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_windowless_kwargs(),
        )
    except OSError as exc:
        return -1, f"could not start ffmpeg: {exc}"

    if process.stdout is not None:
        with contextlib.suppress(OSError, ValueError):
            for raw in process.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    with contextlib.suppress(ProcessLookupError, OSError):
                        process.kill()
                    with contextlib.suppress(OSError, ValueError):
                        process.wait(timeout=5)
                    return -1, "cancelled"
                seconds = _parse_progress_line(raw)
                if seconds is not None and progress_cb is not None:
                    progress_cb(seconds)
    stderr_tail = ""
    if process.stderr is not None:
        with contextlib.suppress(OSError, ValueError):
            stderr_tail = process.stderr.read().decode(errors="replace").strip()[-400:]
    return process.wait(), stderr_tail


def create_preview_proxy(
    src: str | Path,
    dst: str | Path,
    *,
    locator: FFmpegLocator | None = None,
    max_seconds: int | None = None,
    max_width: int = 640,
    progress_cb=None,
    cancel_event=None,
) -> Path:
    """Build a playable preview proxy for *src*; returns the proxy path.

    Encoders are tried best-first against what this ffmpeg actually supports, so
    an LGPL build lands on libopenh264 without wasting a failed libx264 run.
    """
    ffmpeg = (locator or FFmpegLocator()).ffmpeg()
    destination = Path(dst)
    last_error = ""
    for encoder in preview_encoders(ffmpeg):
        command = build_preview_proxy_command(
            ffmpeg,
            src,
            destination,
            video_encoder=encoder,
            max_seconds=max_seconds,
            max_width=max_width,
        )
        code, last_error = _run_ffmpeg_sync(
            command,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )
        if code == 0 and destination.exists() and destination.stat().st_size > 0:
            return destination
        with contextlib.suppress(OSError):
            destination.unlink(missing_ok=True)
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
    raise FFmpegRunError(last_error or "ffmpeg did not produce a preview proxy")


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
            **_windowless_kwargs(),
        )
        stdout, _stderr = await asyncio.wait_for(result.communicate(), timeout=30)
        if result.returncode != 0:
            log.warning("ffprobe_nonzero", path=str(path), code=result.returncode)
            return None
    except (TimeoutError, OSError) as exc:
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
    return None if math.isnan(parsed) else parsed


def _safe_int(value: object) -> int | None:
    if value is None or value in ("", "N/A"):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return None if parsed == 0 else parsed  # 0 typically means "unknown"
