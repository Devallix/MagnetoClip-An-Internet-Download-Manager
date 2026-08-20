"""Fast-resume state persistence for torrent downloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TorrentResumeState:
    """Persistent state for a torrent download, saved as a JSON sidecar."""

    download_id: int
    magnet_uri: str | None = None
    torrent_file_path: str | None = None
    save_dir: str = ""
    filename: str = ""
    info_hash: str = ""
    sequential: bool = False
    file_priorities: list[int] | None = None
    seed_mode: bool = False
    resume_data_b64: str | None = None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> TorrentResumeState | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "magnet_uri": self.magnet_uri,
            "torrent_file_path": self.torrent_file_path,
            "save_dir": self.save_dir,
            "filename": self.filename,
            "info_hash": self.info_hash,
            "sequential": self.sequential,
            "file_priorities": self.file_priorities,
            "seed_mode": self.seed_mode,
            "resume_data_b64": self.resume_data_b64,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TorrentResumeState:
        return cls(
            download_id=data.get("download_id", 0),
            magnet_uri=data.get("magnet_uri"),
            torrent_file_path=data.get("torrent_file_path"),
            save_dir=data.get("save_dir", ""),
            filename=data.get("filename", ""),
            info_hash=data.get("info_hash", ""),
            sequential=data.get("sequential", False),
            file_priorities=data.get("file_priorities"),
            seed_mode=data.get("seed_mode", False),
            resume_data_b64=data.get("resume_data_b64"),
        )


def sidecar_path_for(save_path: str) -> Path:
    """Return the .mctorrent sidecar path for a given download save path."""
    return Path(save_path + ".mctorrent")


def has_resume(save_path: str) -> bool:
    return sidecar_path_for(save_path).exists()


def load_resume(save_path: str) -> TorrentResumeState | None:
    return TorrentResumeState.load(sidecar_path_for(save_path))


def save_resume(state: TorrentResumeState, save_path: str) -> None:
    state.save(sidecar_path_for(save_path))
