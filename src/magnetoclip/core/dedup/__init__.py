"""Duplicate detection: index downloaded files and catch re-downloads."""

from magnetoclip.core.dedup.manager import DedupManager
from magnetoclip.core.dedup.types import DedupStats, DuplicateResult

__all__ = ["DedupManager", "DedupStats", "DuplicateResult"]
