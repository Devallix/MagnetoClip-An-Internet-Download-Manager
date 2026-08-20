"""Type definitions for the torrent download subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TorrentSpec:
    """Everything the torrent handler needs to download one torrent."""

    download_id: int
    save_dir: Path
    filename: str
    magnet_uri: str | None = None
    torrent_file_path: str | None = None
    sequential: bool = False
    file_priorities: list[int] | None = None
    upload_limit: int = 0
    download_limit: int = 0
    seed_mode: bool = False
    paused: bool = False

    @property
    def final_path(self) -> Path:
        return self.save_dir / self.filename


@dataclass
class TorrentStatus:
    """Runtime status snapshot for a single torrent."""

    download_id: int
    state: str = "queued"
    progress: float = 0.0
    downloaded: int = 0
    total: int = 0
    upload_speed: int = 0
    download_speed: int = 0
    num_peers: int = 0
    num_seeds: int = 0
    num_pieces: int = 0
    piece_size: int = 0
    ratio: float = 0.0
    eta_seconds: float | None = None
    all_time_download: int = 0
    all_time_upload: int = 0
    info_hash: str = ""
    name: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "state": self.state,
            "progress": self.progress,
            "downloaded": self.downloaded,
            "total": self.total,
            "upload_speed": self.upload_speed,
            "download_speed": self.download_speed,
            "num_peers": self.num_peers,
            "num_seeds": self.num_seeds,
            "num_pieces": self.num_pieces,
            "piece_size": self.piece_size,
            "ratio": self.ratio,
            "eta_seconds": self.eta_seconds,
            "all_time_download": self.all_time_download,
            "all_time_upload": self.all_time_upload,
            "info_hash": self.info_hash,
            "name": self.name,
            "error": self.error,
        }
