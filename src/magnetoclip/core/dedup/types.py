"""Type definitions for the duplicate-detection feature."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class DuplicateResult:
    """Outcome of a duplicate check for a candidate file.

    ``is_duplicate`` is True only when the file is already indexed with the
    same content hash (an exact duplicate). ``size_match`` records whether a
    same-size file was found, which is a strong hint even before hashing.
    """

    is_duplicate: bool = False
    size_match: bool = False
    existing_path: str | None = None
    existing_filename: str | None = None
    existing_size: int | None = None
    existing_categories: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "is_duplicate": self.is_duplicate,
            "size_match": self.size_match,
            "existing_path": self.existing_path,
            "existing_filename": self.existing_filename,
            "existing_size": self.existing_size,
            "existing_categories": list(self.existing_categories),
        }


@dataclass(slots=True)
class DedupStats:
    """Aggregate statistics over the file-hash index."""

    indexed_files: int = 0
    indexed_bytes: int = 0
    size_groups: int = 0

    def as_dict(self) -> dict:
        return {
            "indexed_files": self.indexed_files,
            "indexed_bytes": self.indexed_bytes,
            "size_groups": self.size_groups,
        }
