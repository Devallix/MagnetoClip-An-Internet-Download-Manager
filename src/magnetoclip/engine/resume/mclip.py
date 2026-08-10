from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class SegmentState:
    index: int
    start: int
    end: int | None
    written: int = 0
    attempts: int = 0
    status: str = "pending"  # pending/active/completed/failed

    @property
    def length(self) -> int | None:
        if self.end is None:
            return None
        return self.end - self.start + 1

    @property
    def complete(self) -> bool:
        length = self.length
        if length is None:
            return False
        return self.written >= length


@dataclass
class MClipState:
    """Serialized state of a download, stored as ``<file>.mclip``."""

    url: str
    file_path: str
    total_size: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    hash_algo: str | None = None
    hash_expected: str | None = None
    hash_calculated: str | None = None
    state: str = "queued"
    segments: list[SegmentState] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.file_path)

    def part_path(self, index: int) -> Path:
        return Path(f"{self.file_path}.part{index}")

    @property
    def part_paths(self) -> list[Path]:
        return [self.part_path(segment.index) for segment in self.segments]

    @property
    def bytes_downloaded(self) -> int:
        return sum(segment.written for segment in self.segments)

    @property
    def complete_segments(self) -> int:
        return sum(1 for segment in self.segments if segment.complete)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "file_path": self.file_path,
            "total_size": self.total_size,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "headers": self.headers,
            "hash_algo": self.hash_algo,
            "hash_expected": self.hash_expected,
            "hash_calculated": self.hash_calculated,
            "state": self.state,
            "segments": [asdict(segment) for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MClipState:
        segments = [
            SegmentState(**segment) for segment in data.get("segments", [])
        ]
        return cls(
            url=data["url"],
            file_path=data["file_path"],
            total_size=data.get("total_size"),
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            headers=data.get("headers") or {},
            hash_algo=data.get("hash_algo"),
            hash_expected=data.get("hash_expected"),
            hash_calculated=data.get("hash_calculated"),
            state=data.get("state", "queued"),
            segments=segments,
        )

    def save(self) -> None:
        sidecar = Path(f"{self.file_path}.mclip")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, sidecar: Path) -> MClipState:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def sidecar_for(cls, file_path: str) -> Path:
        return Path(f"{file_path}.mclip")


def reconcile_part_sizes(state: MClipState) -> None:
    """Sync segment ``written`` counts with actual on-disk part file sizes."""
    for segment in state.segments:
        part = state.part_path(segment.index)
        if part.exists():
            size = part.stat().st_size
            length = segment.length
            if length is not None:
                segment.written = min(size, length)
            else:
                segment.written = size
            if segment.complete:
                segment.status = "completed"
