"""Data models for the YouTube playlist flow.

These are plain Python objects, not ORM models: the extraction pipeline works
with them in memory, and persistence to the database happens through the
:mod:`magnetoclip.media.youtube.controller`/``manager`` layers using the
existing SQLAlchemy tables.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class YouTubeURLType(str, enum.Enum):
    """What a pasted URL refers to, decided without any network access.

    * ``video`` — a single video (``youtu.be/<id>`` or ``/watch?v=<id>``).
    * ``playlist`` — a playlist page (``/playlist?list=…``).
    * ``video_with_playlist`` — a video that explicitly belongs to a playlist
      or mix (``/watch?v=…&list=…``). It downloads as one video but should be
      offered the playlist flow so the surrounding entries are discoverable.
    * ``unknown`` — anything that is not a YouTube URL we recognise.
    """

    video = "video"
    playlist = "playlist"
    video_with_playlist = "video_with_playlist"
    unknown = "unknown"


class PlaylistItemStatus(str, enum.Enum):
    """Lifecycle of one playlist entry during the preview/queue process.

    * ``queued`` — listed in the extracted playlist.
    * ``pending`` — waiting to be turned into a download.
    * ``selected`` — ticked in the preview dialog.
    * ``skipped`` — unticked / already downloaded before (archive hit).
    * ``added`` — a download job was created for it.
    * ``failed`` — extraction or download failed.
    """

    queued = "queued"
    pending = "pending"
    selected = "selected"
    skipped = "skipped"
    added = "added"
    failed = "failed"


@dataclass
class PlaylistVideo:
    """A single entry inside a YouTube playlist."""

    video_id: str
    title: str
    url: str
    index: int = 0
    duration: float | None = None
    status: PlaylistItemStatus = PlaylistItemStatus.queued

    def __post_init__(self) -> None:
        if not self.id_ok():
            raise ValueError(f"invalid YouTube video id: {self.video_id!r}")

    def id_ok(self) -> bool:
        ident = (self.video_id or "").strip()
        return bool(ident) and all(c.isalnum() or c in "-_." for c in ident)


@dataclass
class YouTubePlaylist:
    """The result of a flat extraction: the playlist header + its videos."""

    playlist_id: str
    title: str
    url: str
    video_count: int
    videos: list[PlaylistVideo] = field(default_factory=list)

    def selected(self) -> list[PlaylistVideo]:
        """Videos whose status is :attr:`PlaylistItemStatus.selected`."""
        return [v for v in self.videos if v.status == PlaylistItemStatus.selected]

    def watch_url(self) -> str:
        """A canonical watch URL for this playlist (useful for the archive)."""
        return f"https://www.youtube.com/playlist?list={self.playlist_id}"


__all__ = [
    "PlaylistItemStatus",
    "PlaylistVideo",
    "YouTubePlaylist",
    "YouTubeURLType",
]