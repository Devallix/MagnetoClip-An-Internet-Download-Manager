"""PreviewResolver: determine whether a file is previewable and how."""

from __future__ import annotations

import enum
from pathlib import Path

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tiff", ".tif", ".heic", ".heif", ".avif"}
_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp"}
_AUDIO_EXT = {".mp3", ".flac", ".ogg", ".wav", ".aac", ".m4a", ".opus", ".wma"}
_PDF_EXT = {".pdf"}
_TORRENT_EXT = {".torrent"}
_TEXT_EXT = {".txt", ".json", ".csv", ".xml", ".log", ".md", ".py", ".html", ".js", ".css", ".ts", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".bat", ".ps1", ".sql", ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp"}

_TEXT_MAX_BYTES = 5 * 1024 * 1024


class PreviewType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"
    TORRENT = "torrent"
    TEXT = "text"
    NONE = "none"


class PreviewResolver:
    """Maps a file path to a :class:`PreviewType` it can handle."""

    def __init__(self, context) -> None:
        self.context = context

    def _enabled(self) -> bool:
        return bool(self.context.settings.get("preview.enabled", True))

    def resolve(self, path: str | Path | None) -> PreviewType:
        """Return the preview type for *path*, or ``NONE`` if not previewable."""
        if not path:
            return PreviewType.NONE
        if not self._enabled():
            return PreviewType.NONE
        path = Path(path)
        if not path.is_file():
            return PreviewType.NONE
        suffix = path.suffix.lower()
        if suffix in _IMAGE_EXT:
            return PreviewType.IMAGE
        if suffix in _VIDEO_EXT:
            return PreviewType.VIDEO
        if suffix in _AUDIO_EXT:
            return PreviewType.AUDIO
        if suffix in _PDF_EXT:
            return PreviewType.PDF
        if suffix in _TORRENT_EXT:
            return PreviewType.TORRENT
        if suffix in _TEXT_EXT:
            try:
                if path.stat().st_size <= _TEXT_MAX_BYTES:
                    return PreviewType.TEXT
            except OSError:
                return PreviewType.NONE
            return PreviewType.NONE
        return PreviewType.NONE

    def filenames(self, download) -> list[str]:
        """Candidate on-disk paths for a download (final + media)."""
        candidates: list[Path] = []
        save_path = getattr(download, "save_path", None)
        if save_path:
            candidates.append(Path(save_path))
        media = getattr(download, "media_metadata_json", None) or {}
        media_path = media.get("path")
        if media_path:
            candidates.append(Path(media_path))
        if (
            getattr(download, "detected_type", None) == "torrent"
            or str(save_path or "").lower().endswith(".torrent")
        ):
            self._add_torrent_candidates(save_path, candidates)
            url = getattr(download, "url", None)
            if url and str(url).lower().endswith(".torrent"):
                src = Path(url)
                if src.is_file():
                    candidates.append(src)
        seen: set[str] = set()
        result: list[str] = []
        for p in candidates:
            key = str(p)
            if key not in seen:
                seen.add(key)
                result.append(key)
        return result

    @staticmethod
    def _add_torrent_candidates(save_path, candidates: list[Path]) -> None:
        """Add .torrent metadata files in the save directory to *candidates*.

        Torrent downloads keep their metadata in the download folder under the
        record's filename (or a ``.torrent_*.tmp`` temp name from older
        downloads). Either file lets a completed torrent be previewed even when
        the canonical ``save_path`` was never written.
        """
        parent = Path(save_path).parent if save_path else None
        if parent is None or not parent.is_dir():
            return
        for child in sorted(parent.iterdir()):
            if not child.is_file():
                continue
            if child.suffix.lower() == ".torrent" or child.name.startswith(".torrent_") and child.suffix.lower() == ".tmp":
                candidates.append(child)

    def type_of_download(self, download) -> PreviewType:
        """Best preview type across a download's candidate paths."""
        for candidate in self.filenames(download):
            resolved = self.resolve(candidate)
            if resolved != PreviewType.NONE:
                return resolved
        return PreviewType.NONE
