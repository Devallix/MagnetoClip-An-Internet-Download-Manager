"""Format-selection model for the YouTube multi-resolution download flow.

Operates on the raw yt-dlp ``formats`` entries produced by metadata
extraction (no orm / no ffprobe here): it classifies the ladder, builds the
per-resolution quality options the GUI presents, and generates the exact
yt-dlp ``-f`` selectors that the generic streaming pipeline should use.

Selector strategy (never hard-coded format ids, never ``-f best``):

* Per height ``N`` we prefer a single-file progressive/combined format:
  ``b[height<=N]/b`` — best single file with height at or below ``N``,
  falling back to the overall best single file if none match. This is the
  "lower if unavailable" behaviour: yt-dlp never upgrades past ``N``.
* When no combined format exists at ``N`` but audio tracks are available,
  and ffmpeg can merge, we use ``bv[height<=N]+ba/b``.
* Best Available uses ``bv+ba/b`` (or a combined-only fallback when ffmpeg
  is missing).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

# Preferred container order when describing a format to the user.
EXTENSION_PREFERENCE: tuple[str, ...] = (
    "mp4",
    "m4a",
    "mov",
    "webm",
    "mkv",
    "flv",
    "ts",
)


class VideoResolution(int, enum.Enum):
    """A standard YouTube resolution step ('p height' of the video frame)."""

    R144 = 144
    R240 = 240
    R360 = 360
    R480 = 480
    R720 = 720
    R1080 = 1080
    R1440 = 1440
    R2160 = 2160
    R4320 = 4320

    def label(self) -> str:
        return display_height(self.value)

    @classmethod
    def from_height(cls, height: int | None) -> VideoResolution | None:
        """Nearest ladder step at or below *height* (``None`` when unusable)."""
        if not height or height < 100:
            return None
        for step in sorted(cls, key=lambda s: s.value, reverse=True):
            if height >= step.value:
                return step
        return None


class QualityFallbackPolicy(str, enum.Enum):
    """What to do when the requested resolution is not available.

    The selectors we generate implement :attr:`LOWER` (never upgrade): if no
    format reaches the requested height, yt-dlp picks the best format below
    it, leaving total freedom to the platform's own render ladder.
    """

    LOWER = "lower"
    HIGHEST = "highest"


def display_height(height: int | None) -> str:
    """Human label for a height: 1080 → ``1080p``, 2160 → ``4K`` ..."""
    if not height:
        return "Best Available"
    if height >= 4200:
        return "8K"
    if height >= 2000:
        return "4K"
    if height >= 1400:
        return "1440p"
    return f"{height}p"


def format_note_label(height: int | None) -> str:
    """Compact quality key used in settings/Db columns (e.g. ``1080``)."""
    return str(height) if height else "best"


@dataclass(frozen=True)
class YouTubeFormat:
    """A single entry of a yt-dlp ``formats`` list, normalised for the UI."""

    format_id: str
    ext: str = ""
    height: int | None = None
    width: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    protocol: str | None = None
    dynamic_range: str | None = None
    tbr: float | None = None
    filesize: int | None = None
    filesize_approx: int | None = None

    @property
    def has_video(self) -> bool:
        return self.vcodec not in (None, "", "none")

    @property
    def has_audio(self) -> bool:
        return self.acodec not in (None, "", "none")

    @property
    def is_combined(self) -> bool:
        return self.has_video and self.has_audio

    @property
    def video_only(self) -> bool:
        return self.has_video and not self.has_audio

    @property
    def audio_only(self) -> bool:
        return self.has_audio and not self.has_video

    @property
    def size(self) -> int | None:
        return self.filesize if self.filesize else self.filesize_approx

    @property
    def is_hdr(self) -> bool:
        return bool(self.dynamic_range and self.dynamic_range.lower() != "sdr")

    @classmethod
    def from_ydl(cls, fmt: dict) -> "YouTubeFormat":
        return cls(
            format_id=str(fmt.get("format_id") or str(fmt.get("format_index") or "")),
            ext=fmt.get("ext") or "",
            height=fmt.get("height"),
            width=fmt.get("width"),
            fps=fmt.get("fps"),
            vcodec=fmt.get("vcodec"),
            acodec=fmt.get("acodec"),
            protocol=fmt.get("protocol"),
            dynamic_range=fmt.get("dynamic_range"),
            tbr=fmt.get("tbr"),
            filesize=fmt.get("filesize"),
            filesize_approx=fmt.get("filesize_approx"),
        )


def classify_formats(info: dict) -> list[YouTubeFormat]:
    """Normalise a yt-dlp info dict into usable :class:`YouTubeFormat`s.

    Entries without a format id, without any stream url, or that are pure
    storyboards are dropped. Duplicate ids are collapsed keeping the first.
    """
    seen: set[str] = set()
    out: list[YouTubeFormat] = []
    for raw in info.get("formats") or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("url"):
            continue
        fmt = YouTubeFormat.from_ydl(raw)
        if not fmt.format_id or fmt.format_id in seen:
            continue
        seen.add(fmt.format_id)
        out.append(fmt)
    return out


@dataclass
class QualityOption:
    """One selectable resolution in the quality dialog / playlist combo."""

    requested_height: int | None  # None => Best Available
    label: str
    format_selector: str
    requires_merge: bool
    merge_output_format: str | None = "mp4"
    estimated_size: int | None = None
    size_estimated: bool = False
    format_label: str = ""
    format_id: str | None = None

    @property
    def quality_key(self) -> str:
        return format_note_label(self.requested_height)


@dataclass
class SelectedYouTubeQuality:
    """The concrete choice made for one download, ready to persist."""

    requested: str
    height: int | None
    ffmpeg_required: bool
    format_selector: str
    merge_output_format: str | None
    format_label: str = ""
    format_id: str | None = None
    estimated_size: int | None = None
    policy: QualityFallbackPolicy = QualityFallbackPolicy.LOWER

    def to_record(self) -> dict:
        return {
            "requested_quality": self.requested,
            "height": self.height,
            "ffmpeg_required": self.ffmpeg_required,
            "format_selector": self.format_selector,
            "merge_output_format": self.merge_output_format,
            "format_label": self.format_label,
            "format_id": self.format_id,
            "estimated_size": self.estimated_size,
        }


def preferred_ext(fmts: list[YouTubeFormat]) -> str:
    """The friendliest container among *fmts* (mp4 > m4a > mov > webm ...)."""
    for pref in EXTENSION_PREFERENCE:
        for fmt in fmts:
            if fmt.ext == pref:
                return pref
    for fmt in fmts:
        if fmt.ext:
            return fmt.ext
    return "mp4"


def _rank(fmt: YouTubeFormat) -> tuple:
    return (
        fmt.tbr is not None,
        fmt.tbr or 0,
        fmt.height or 0,
        fmt.fps or 0,
    )


def _sum_sizes(fmts: list[YouTubeFormat]) -> int | None:
    total = 0
    saw = False
    for fmt in fmts:
        size = fmt.size
        if size is None:
            return None
        total += size
        saw = True
    return total if saw else None


def describe_format(fmts: list[YouTubeFormat], needs_merge: bool) -> str:
    """Human summary like ``MP4 · 1080p · 60fps · HDR`` for the dialog."""
    if not fmts:
        return ""
    fmt = max(fmts, key=_rank)
    parts = [preferred_ext(fmts).upper()]
    if fmt.height:
        parts.append(display_height(fmt.height))
    if fmt.fps and int(fmt.fps) >= 30:
        parts.append(f"{int(round(fmt.fps))}fps")
    if fmt.is_hdr:
        parts.append("HDR")
    if needs_merge:
        parts.append("merge")
    return " · ".join(parts)


def build_format_selector(
    height: int | None,
    *,
    audio_available: bool = True,
    ffmpeg_available: bool = True,
    policy: QualityFallbackPolicy = QualityFallbackPolicy.LOWER,
) -> str:
    """Generate the yt-dlp ``-f`` selector for the requested *height*.

    *height* ``None``/0 means Best Available. As long as ffmpeg can merge
    audio we always prefer the ``bv[height<=N]+ba/b`` merge branch: on modern
    ladders high resolutions are video-only, and the single-file ``b`` form
    would silently cap to the best *progressive* format (often 360p/720p).
    The single-file branch ``b[acodec!=none][vcodec!=none][height<=N]/b`` is
    reserved for the no-ffmpeg degradation. ``policy`` only influences the
    fallback tail.
    """
    if not height:
        if ffmpeg_available:
            return "bv+ba/b"
        return "b[acodec!=none][vcodec!=none]/b"
    ff = f"[height<={height}]"
    if ffmpeg_available and audio_available:
        return f"bv{ff}+ba/b"
    # No way to merge audio: let yt-dlp choose the best single file at or
    # below the requested height instead of erroring.
    return f"b[acodec!=none][vcodec!=none]{ff}/b"


def _best_formats(
    fmts: list[YouTubeFormat], *, height: int | None = None
) -> list[YouTubeFormat]:
    def matches(fmt: YouTubeFormat) -> bool:
        if height is None:
            return True
        return fmt.height is not None and fmt.height <= height

    return [f for f in fmts if matches(f)]


class YouTubeFormatAnalyzer:
    """Turns a raw format ladder into presentation-ready quality options."""

    def __init__(
        self, formats: list[YouTubeFormat], *, duration: float | None = None
    ) -> None:
        self.formats = formats
        self.duration = duration
        self.combined = [f for f in formats if f.is_combined]
        self.video_only = [f for f in formats if f.video_only]
        self.audio = [f for f in formats if f.audio_only]

    @property
    def available_heights(self) -> list[int]:
        heights = {
            f.height for f in (self.combined + self.video_only) if f.height
        }
        return sorted(heights, reverse=True)

    def best_height(self) -> int | None:
        heights = self.available_heights
        return heights[0] if heights else None

    def combined_at(self, height: int) -> list[YouTubeFormat]:
        return [f for f in self.combined if f.height == height]

    def video_at(self, height: int) -> list[YouTubeFormat]:
        return [f for f in self.video_only if f.height == height]

    def build_options(
        self, *, ffmpeg_available: bool = True
    ) -> list[QualityOption]:
        """Quality ladder, best first. Merge options dropped when ffmpeg is
        unavailable so the UI never offers a download that would fail."""
        options: list[QualityOption] = []
        options.append(
            self._option(None, ffmpeg_available=ffmpeg_available)
        )
        for height in self.available_heights:
            option = self._option(height, ffmpeg_available=ffmpeg_available)
            if option is not None:
                options.append(option)
        return options

    def _option(
        self, height: int | None, *, ffmpeg_available: bool
    ) -> QualityOption | None:
        audio_for_est: list[YouTubeFormat] | None = None
        if height is None:
            audio_ok = bool(self.audio)
            video_only_ok = bool(self.video_only)
            needs_merge = ffmpeg_available and video_only_ok and audio_ok
            selector = build_format_selector(
                None,
                audio_available=audio_ok,
                ffmpeg_available=ffmpeg_available,
            )
            # bv+ba/b prefers merged video+audio when both branches exist;
            # otherwise yt-dlp falls back to the best single combined file.
            if needs_merge:
                shown = [max(self.video_only, key=_rank), max(self.audio, key=_rank)]
            else:
                shown = self.combined or self.video_only
            label = "Best Available"
            format_id = _option_format_id(shown, needs_merge)
        else:
            combined_exists = bool(self.combined_at(height))
            if not combined_exists and (not ffmpeg_available or not bool(self.audio)):
                # No single-file candidate at this height and no way to merge:
                # offering it would fail or silently ignore the resolution.
                return None
            needs_merge = ffmpeg_available and bool(self.audio)
            selector = build_format_selector(
                height,
                audio_available=bool(self.audio),
                ffmpeg_available=ffmpeg_available,
            )
            shown = self.combined_at(height) or self.video_at(height)
            label = display_height(height)
            format_id = _option_format_id(shown + self.audio, needs_merge)
            if needs_merge:
                audio_for_est = self.audio

        if not shown:
            return None

        estimate, estimated = self._estimate(shown, audio_for_est)
        return QualityOption(
            requested_height=height,
            label=label,
            format_selector=selector,
            requires_merge=needs_merge,
            merge_output_format="mp4" if needs_merge else None,
            estimated_size=estimate,
            size_estimated=estimated,
            format_label=describe_format(shown, needs_merge),
            format_id=format_id,
        )

    def _size_bytes(self, fmt: YouTubeFormat) -> int | None:
        """Exact size, or a bitrate × duration estimate when yt-dlp omits it."""
        if fmt.size:
            return fmt.size
        if fmt.tbr and self.duration:
            # yt-dlp tbr is in kilobits/sec (K = 1000).
            return round(fmt.tbr * 1000.0 / 8.0 * float(self.duration))
        return None

    def _estimate(
        self, video: list[YouTubeFormat], audio: list[YouTubeFormat] | None
    ) -> tuple[int | None, bool]:
        chosen = [max(video, key=_rank)] if video else []
        if audio:
            chosen.append(max(audio, key=_rank))
        if not chosen:
            return None, False
        exact = _sum_sizes(chosen)
        if exact is not None:
            return exact, False
        sizes = [self._size_bytes(f) for f in chosen]
        if any(size is None for size in sizes):
            return None, False
        return sum(size for size in sizes if size is not None), True

    def select(
        self, height: int | None, *, ffmpeg_available: bool = True
    ) -> SelectedYouTubeQuality | None:
        """Direct selection (no dialog): playlist flow / settings default."""
        options = self.build_options(ffmpeg_available=ffmpeg_available)
        if height is None:
            target = options[0] if options else None
        else:
            target = next((o for o in options[1:] if o.requested_height == height), None)
            if target is None:
                lower = [o for o in options[1:] if (o.requested_height or 0) < height]
                target = lower[0] if lower else (options[0] if options else None)
        if target is None:
            return None
        return SelectedYouTubeQuality(
            requested=target.label,
            height=target.requested_height,
            ffmpeg_required=target.requires_merge,
            format_selector=target.format_selector,
            merge_output_format=target.merge_output_format,
            format_label=target.format_label,
            format_id=target.format_id,
            estimated_size=target.estimated_size,
        )


def build_standalone_selector(
    quality: str | int, *, ffmpeg_available: bool = True
) -> str:
    """Selector for the playlist path, derived without per-video metadata.

    ``quality`` may be a height int, ``"best"`` (Best Available) or an
    ``"audio"`` request. yt-dlp's ``[height<=N]`` implements the lower-if-
    unavailable fallback, so per-video metadata is not required up front.
    """
    if quality == "audio":
        return "bestaudio/best"
    if not quality or quality == "best":
        return build_format_selector(None, ffmpeg_available=ffmpeg_available)
    try:
        height = int(quality)
    except (TypeError, ValueError):
        return build_format_selector(None, ffmpeg_available=ffmpeg_available)
    return build_format_selector(
        height, audio_available=True,
        ffmpeg_available=ffmpeg_available,
    )


def _option_format_id(fmts: list[YouTubeFormat], needs_merge: bool) -> str | None:
    if not fmts:
        return None
    if needs_merge:
        video = max((f for f in fmts if f.has_video), key=_rank, default=None)
        audio = max((f for f in fmts if f.audio_only), key=_rank, default=None)
        if not video:
            return None
        return f"{video.format_id}+{audio.format_id}" if audio else video.format_id
    fmt = max(fmts, key=_rank)
    return fmt.format_id


__all__ = [
    "EXTENSION_PREFERENCE",
    "QualityFallbackPolicy",
    "QualityOption",
    "SelectedYouTubeQuality",
    "VideoResolution",
    "YouTubeFormat",
    "YouTubeFormatAnalyzer",
    "build_format_selector",
    "build_standalone_selector",
    "classify_formats",
    "display_height",
    "format_note_label",
    "preferred_ext",
]