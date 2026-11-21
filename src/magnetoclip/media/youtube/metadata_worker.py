"""Threaded YouTube video-metadata/quality-ladder extraction.

The quality dialog must show *the real ladder* before asking the user to pick
a resolution, and yt-dlp extraction is blocking, so it runs on a ``QThread``
exactly like the playlist worker (:mod:`magnetoclip.media.youtube.worker`).
The worker is a plain ``QObject`` that never touches the UI.

Unlike the older download-time resolution (which used the android player
client and got capped to 360p), both metadata extraction and the download
use yt-dlp's default web player client with *no* ``format`` key, so every
render (including 2160p/8K and HDR-only steps) is present in the ladder and
the download can reach exactly what the user picked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..streaming import is_bot_wall_error
from .formats import (
    QualityOption,
    YouTubeFormat,
    YouTubeFormatAnalyzer,
    classify_formats,
)


@dataclass
class VideoMetadataResult:
    """What the quality dialog needs from an extraction, all in memory."""

    url: str
    title: str
    ext: str
    media_type: str
    duration: float | None
    formats: list[YouTubeFormat] = field(default_factory=list)
    options: list[QualityOption] = field(default_factory=list)
    ffmpeg_available: bool = True

    @property
    def best_height(self) -> int | None:
        return YouTubeFormatAnalyzer(self.formats).best_height()


class YouTubeMetadataExtractor:
    """Blocking yt-dlp metadata extraction (no download, all formats)."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def extract(
        self,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from yt_dlp import YoutubeDL

        base_params: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": self.timeout,
            "nocheckcertificate": True,
        }
        _apply_impersonation(base_params)
        _apply_manifest_token(base_params, url)
        if headers:
            base_params["http_headers"] = dict(headers)

        info: dict[str, Any] | None = None
        browser_name = _detect_browser()
        if cookies:
            attempts: tuple = (None, cookies)
        elif browser_name:
            attempts = (None, "_browser_")
        else:
            attempts = (None,)
        for attempt_cookies in attempts:
            use_cookies = attempt_cookies if attempt_cookies != "_browser_" else None
            cookiefile = _write_cookiefile(url, use_cookies)
            params = dict(base_params)
            if cookiefile is not None:
                params["cookiefile"] = str(cookiefile)
            elif attempt_cookies == "_browser_" and browser_name:
                params["cookiesfrombrowser"] = (browser_name,)
            try:
                with YoutubeDL(params) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                message = str(exc)
                can_retry = (
                    attempt_cookies is None
                    and (bool(cookies) or browser_name)
                    and is_bot_wall_error(message)
                )
                if not can_retry:
                    raise
            finally:
                if cookiefile is not None:
                    try:
                        cookiefile.unlink(missing_ok=True)
                    except OSError:
                        pass
            if info is not None:
                break

        if not info:
            raise RuntimeError("yt-dlp returned no video info")
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise RuntimeError("Playlist contains no entries")
            info = dict(entries[0])
        return info


class YouTubeMetadataWorker(QObject):
    """Runs :class:`YouTubeMetadataExtractor` on a worker thread.

    Signals:
        started() — extraction began.
        metadata_ready(result) — :class:`VideoMetadataResult` for the dialog.
        error(message) — extraction failed (user-facing text).
    """

    started = Signal()
    metadata_ready = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        extractor_factory=None,
        ffmpeg_available: bool = True,
    ) -> None:
        super().__init__(parent)
        self._params: dict[str, Any] | None = None
        self._extractor_factory = extractor_factory or YouTubeMetadataExtractor
        self._ffmpeg_available = ffmpeg_available

    def queue(
        self,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._params = {"url": url, "cookies": cookies, "headers": headers}

    @Slot()
    def run(self) -> None:
        params = self._params
        if not params:
            self.error.emit("no extraction pending")
            return
        self.started.emit()
        try:
            extractor = self._extractor_factory()
            info = extractor.extract(
                params["url"],
                cookies=params.get("cookies"),
                headers=params.get("headers"),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.error.emit(f"failed to read YouTube video info: {exc}")
            return

        result = build_metadata_result(
            info,
            url=params["url"],
            ffmpeg_available=self._ffmpeg_available,
        )
        self.metadata_ready.emit(result)


def build_metadata_result(
    info: dict[str, Any], *, url: str, ffmpeg_available: bool = True
) -> VideoMetadataResult:
    """Turn a raw yt-dlp info dict into a :class:`VideoMetadataResult`."""
    formats = classify_formats(info)
    analyzer = YouTubeFormatAnalyzer(formats, duration=info.get("duration"))
    media_type = "audio" if info.get("vcodec") == "none" else "video"
    return VideoMetadataResult(
        url=url,
        title=str(info.get("title") or "stream"),
        ext=info.get("ext") or "mp4",
        media_type=media_type,
        duration=info.get("duration"),
        formats=formats,
        options=analyzer.build_options(ffmpeg_available=ffmpeg_available),
        ffmpeg_available=ffmpeg_available,
    )


class YouTubeMetadataController(QObject):
    """Owns one background metadata extraction, translated to UI signals."""

    metadata_started = Signal()
    metadata_finished = Signal(object)
    metadata_failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: YouTubeMetadataWorker | None = None
        self.probe: VideoMetadataResult | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def extract(
        self,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        ffmpeg_available: bool = True,
    ) -> bool:
        """Start metadata extraction for *url*; ``False`` when already busy."""
        if self.is_running or not url:
            return False

        thread = QThread(self)
        worker = YouTubeMetadataWorker(
            ffmpeg_available=ffmpeg_available,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.started.connect(self._on_started)
        worker.metadata_ready.connect(self._on_ready)
        worker.error.connect(self._on_error)
        worker.metadata_ready.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        self.probe = None
        worker.queue(url, cookies=cookies, headers=headers)
        thread.start()
        return True

    def shutdown(self) -> None:
        self._finish_thread()

    def _finish_thread(self) -> None:
        thread = self._thread
        self._thread = None
        self._worker = None
        if thread is None:
            return
        if thread.isRunning():
            thread.quit()
            thread.wait(3000)
        thread.deleteLater()

    def _on_started(self) -> None:
        self.metadata_started.emit()

    def _on_ready(self, result: VideoMetadataResult) -> None:
        self.probe = result
        self._finish_thread()
        self.metadata_finished.emit(result)

    def _on_error(self, message: str) -> None:
        self._finish_thread()
        self.metadata_failed.emit(message)


def _apply_impersonation(params: dict) -> None:
    from ..streaming import _apply_impersonation as _impl

    _impl(params)


def _apply_manifest_token(params: dict, url: str) -> None:
    from ..streaming import _apply_manifest_token as _impl

    _impl(params, url)


def _detect_browser() -> str | None:
    from ..streaming import _detect_browser as _impl

    return _impl()


def _write_cookiefile(url: str, cookies):
    from ..streaming import _write_cookiefile as _impl

    return _impl(url, cookies)


__all__ = [
    "VideoMetadataResult",
    "YouTubeMetadataController",
    "YouTubeMetadataExtractor",
    "YouTubeMetadataWorker",
    "build_metadata_result",
]