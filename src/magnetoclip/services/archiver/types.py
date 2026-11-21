"""Shared types for the webpage archiver."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ArchiveResource:
    """A single external resource referenced by an archived page."""

    tag: str
    attr: str
    url: str
    data_base64: str = ""
    inline: bool = False
    error: str | None = None


@dataclass(slots=True)
class ArchiveResult:
    """Outcome of archiving a webpage."""

    ok: bool = False
    path: str = ""
    title: str = ""
    url: str = ""
    resources_inlined: int = 0
    resources_failed: int = 0
    size_bytes: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "path": self.path,
            "title": self.title,
            "url": self.url,
            "resources_inlined": self.resources_inlined,
            "resources_failed": self.resources_failed,
            "size_bytes": self.size_bytes,
            "error": self.error,
        }
