"""Persistent YouTube download archive (``media/youtube/archive.py``).

The archive is a plain-text file of YouTube video ids that were already queued
or completed by MagnetoClip. When the same playlist (or a new playlist) is
previewed again, ids present in the archive are shown as "already downloaded"
and pre-skipped, so the user is not offered half a library twice.

The file lives under the app data dir (``data_dir/archives/youtube.txt``); one
id per line. Writes are idempotent and never rewrite the whole file (each new
id is appended), so the helper is safe to call from any thread.
"""

from __future__ import annotations

from pathlib import Path


class PlaylistArchive:
    """Line-based set of already-seen YouTube video ids."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return set()
        return {
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    def contains(self, video_id: str) -> bool:
        return video_id in self._load()

    def ids(self) -> set[str]:
        """All archived video ids."""
        return self._load()

    def add(self, video_id: str) -> None:
        """Record an id if not already present (idempotent, append-only)."""
        video_id = (video_id or "").strip()
        if not video_id:
            return
        if self.contains(video_id):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{video_id}\n")
        except OSError:
            pass

    def add_many(self, video_ids) -> None:
        for video_id in video_ids:
            self.add(video_id)


__all__ = ["PlaylistArchive"]