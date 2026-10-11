"""Application-level media models shared across the streaming pipeline.

These dataclasses are the canonical vocabulary of the streaming subsystem:
what a resolved stream *is* (:class:`MediaInfo`), every downloadable
presentation variant (:class:`MediaFormat`), and the capture-time request
context that must survive from detection to download (:class:`RequestContext`).

yt-dlp's :class:`~magnetoclip.media.streaming.StreamInfo` and the browser
extension captures convert into these models; the HLS analyzer and download
engine consume them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RequestContext:
    """Everything needed to replay a stream fetch after capture time.

    Populated when the browser extension reports a manifest, persisted on the
    ``Download`` row (``headers_json`` + ``stream_context_json``) and handed to
    whichever backend fetches the stream. ``page_url``/``frame_url`` pin the
    exact tab/frame whose network state clears Cloudflare challenges.
    """

    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str | None = None
    referer: str | None = None
    page_url: str | None = None
    frame_url: str | None = None
    captured_at: str | None = None
    source: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_headers(
        cls,
        headers: dict[str, str] | None,
        *,
        cookies: dict[str, str] | None = None,
        user_agent: str | None = None,
        page_url: str | None = None,
        frame_url: str | None = None,
        source: str | None = None,
    ) -> RequestContext:
        headers = dict(headers or {})
        referer = headers.pop("referer", None) or headers.pop("Referer", None)
        ua = user_agent or headers.pop("user-agent", None) or headers.pop("User-Agent", None)
        return cls(
            headers=headers,
            cookies=dict(cookies or {}),
            user_agent=ua,
            referer=referer,
            page_url=page_url,
            frame_url=frame_url,
            captured_at=_now_iso(),
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "headers": dict(self.headers),
            "cookies": dict(self.cookies),
            "user_agent": self.user_agent,
            "referer": self.referer,
            "page_url": self.page_url,
            "frame_url": self.frame_url,
            "captured_at": self.captured_at,
            "source": self.source,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RequestContext:
        data = dict(data or {})
        return cls(
            headers=dict(data.get("headers") or {}),
            cookies=dict(data.get("cookies") or {}),
            user_agent=data.get("user_agent"),
            referer=data.get("referer"),
            page_url=data.get("page_url"),
            frame_url=data.get("frame_url"),
            captured_at=data.get("captured_at"),
            source=data.get("source"),
            extra=dict(data.get("extra") or {}),
        )

    @classmethod
    def from_download(cls, download) -> RequestContext:
        """Rebuild the context persisted on a ``Download`` row.

        ``headers_json`` is the source of truth: captures store ``Referer``
        and ``User-Agent`` there, with cookies merged into the ``Cookie``
        header. Anything richer (page/frame URLs, host cookies) lives in
        ``stream_context_json`` when present.
        """
        headers = dict(download.headers_json or {})
        cookies: dict[str, str] = {}
        cookie_header = headers.pop("cookie", None) or headers.pop("Cookie", None)
        if cookie_header:
            try:
                from ..network.cookies.jar import parse_cookie_header

                cookies = parse_cookie_header(cookie_header) or {}
            except Exception:  # noqa: BLE001 - defensive
                cookies = {}
        stored = cls.from_dict(download.stream_context_json or {})
        merged_headers = dict(stored.headers)
        for key, value in list(headers.items()):
            merged_headers.setdefault(key, value)
        context = cls(
            headers=merged_headers,
            cookies={**stored.cookies, **cookies},
            user_agent=stored.user_agent or headers.pop("User-Agent", None) or None,
            referer=stored.referer or headers.pop("Referer", None) or None,
            page_url=stored.page_url,
            frame_url=stored.frame_url,
            captured_at=stored.captured_at,
            source=stored.source,
            extra=dict(stored.extra),
        )
        if not context.captured_at:
            context.captured_at = _now_iso()
        return context

    def fetch_headers(self) -> dict[str, str]:
        """Headers to replay when fetching this stream (Referer + UA + Cookie)."""
        headers = dict(self.headers)
        if self.referer and "Referer" not in headers:
            headers["Referer"] = self.referer
        if self.user_agent and "User-Agent" not in headers:
            headers["User-Agent"] = self.user_agent
        if self.cookies and "Cookie" not in headers and "cookie" not in headers:
            from ..network.cookies.jar import format_cookie_header

            headers["Cookie"] = format_cookie_header(self.cookies)
        return headers


@dataclass
class MediaFormat:
    """One downloadable variant/presentation of a stream."""

    format_id: str
    ext: str | None = None
    height: int | None = None
    bandwidth: int = 0
    fps: float | None = None
    codecs: str | None = None
    protocol: str | None = None  # "hls", "dash", "https", ...
    video_url: str | None = None
    audio_url: str | None = None
    audio_only: bool = False
    has_video: bool = True
    filesize_est: int | None = None

    @classmethod
    def from_ydl_dict(cls, fmt: dict[str, Any]) -> MediaFormat:
        height = fmt.get("height")
        if isinstance(height, str) and height.isdigit():
            height = int(height)
        if not isinstance(height, int):
            height = None
        fps = fmt.get("fps")
        try:
            fps = float(fps) if fps is not None else None
        except (TypeError, ValueError):
            fps = None
        bandwidth = int(fmt.get("tbr") or fmt.get("abr") or fmt.get("vbr") or 0 or 0)
        audio_only = fmt.get("vcodec") == "none" or fmt.get("height") is None
        return cls(
            format_id=str(fmt.get("format_id") or ""),
            ext=fmt.get("ext"),
            height=height,
            bandwidth=bandwidth,
            fps=fps,
            codecs=fmt.get("codecs") or None,
            protocol=fmt.get("protocol"),
            video_url=fmt.get("url"),
            audio_only=audio_only,
            has_video=not audio_only,
            filesize_est=fmt.get("filesize") or fmt.get("filesize_approx"),
        )

    def label(self) -> str:
        if self.audio_only:
            return "Audio"
        if self.height:
            return f"{self.height}p"
        return "Stream"

    def sort_key(self) -> tuple:
        return (self.height or 0, self.bandwidth)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_id": self.format_id,
            "ext": self.ext,
            "height": self.height,
            "bandwidth": self.bandwidth,
            "fps": self.fps,
            "codecs": self.codecs,
            "protocol": self.protocol,
            "video_url": self.video_url,
            "audio_url": self.audio_url,
            "audio_only": self.audio_only,
            "has_video": self.has_video,
            "filesize_est": self.filesize_est,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MediaFormat:
        data = dict(data or {})
        return cls(
            format_id=str(data.get("format_id") or ""),
            ext=data.get("ext"),
            height=data.get("height"),
            bandwidth=int(data.get("bandwidth") or 0),
            fps=data.get("fps"),
            codecs=data.get("codecs"),
            protocol=data.get("protocol"),
            video_url=data.get("video_url"),
            audio_url=data.get("audio_url"),
            audio_only=bool(data.get("audio_only")),
            has_video=bool(data.get("has_video", True)),
            filesize_est=data.get("filesize_est"),
        )


@dataclass
class MediaInfo:
    """A resolved stream: metadata plus every download variant."""

    url: str
    title: str
    ext: str = "mp4"
    media_type: str = "video"  # "video" | "audio"
    size: int | None = None
    duration: float | None = None
    webpage_url: str | None = None
    formats: list[MediaFormat] = field(default_factory=list)
    context: RequestContext | None = None

    @classmethod
    def from_ydl(
        cls,
        url: str,
        *,
        title: str,
        ext: str = "mp4",
        media_type: str = "video",
        size: int | None = None,
        duration: float | None = None,
        webpage_url: str | None = None,
        formats: list[dict[str, Any]] | None = None,
        context: RequestContext | None = None,
    ) -> MediaInfo:
        return cls(
            url=url,
            title=title,
            ext=ext or "mp4",
            media_type=media_type,
            size=size,
            duration=duration,
            webpage_url=webpage_url or url,
            formats=[MediaFormat.from_ydl_dict(fmt) for fmt in (formats or [])],
            context=context,
        )

    @property
    def best_format(self) -> MediaFormat | None:
        if not self.formats:
            return None
        return max(self.formats, key=lambda fmt: fmt.sort_key())

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "ext": self.ext,
            "media_type": self.media_type,
            "size": self.size,
            "duration": self.duration,
            "webpage_url": self.webpage_url,
            "formats": [fmt.to_dict() for fmt in self.formats],
            "context": self.context.to_dict() if self.context else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MediaInfo:
        data = dict(data or {})
        context = data.get("context")
        return cls(
            url=str(data.get("url") or ""),
            title=str(data.get("title") or "stream"),
            ext=str(data.get("ext") or "mp4"),
            media_type=str(data.get("media_type") or "video"),
            size=data.get("size"),
            duration=data.get("duration"),
            webpage_url=data.get("webpage_url"),
            formats=[MediaFormat.from_dict(fmt) for fmt in (data.get("formats") or [])],
            context=RequestContext.from_dict(context) if context else None,
        )