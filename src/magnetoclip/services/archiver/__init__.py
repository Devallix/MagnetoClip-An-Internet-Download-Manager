"""Webpage archiver: save pages as self-contained HTML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.events.bus import Events
from .archiver import PageArchiver
from .scraper import PageScraper
from .types import ArchiveResult

__all__ = ["ArchiveResult", "PageArchiver", "PageScraper", "WebpageArchiver"]


class WebpageArchiver:
    """Facade for archiving webpages into self-contained HTML files."""

    def __init__(self, context) -> None:
        self.context = context
        self.events = context.events
        self.archiver = PageArchiver(context)

    def archive(
        self,
        url: str,
        save_dir: Path | str,
        *,
        notify: bool = True,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> ArchiveResult:
        result = self.archiver.archive(
            url, Path(save_dir), headers=headers, cookies=cookies
        )
        if result.ok:
            self.events.post(
                Events.ARCHIVE_COMPLETED,
                {
                    "ok": True,
                    "path": result.path,
                    "url": result.url,
                    "title": result.title,
                    "resources_inlined": result.resources_inlined,
                },
            )
        else:
            self.events.post(
                Events.ARCHIVE_COMPLETED,
                {"ok": False, "url": result.url, "error": result.error},
            )
        return result

    def archive_with_metadata(
        self,
        url: str,
        save_dir: Path | str,
        *,
        notify: bool = True,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        **meta: Any,
    ) -> ArchiveResult:
        result = self.archiver.archive_with_metadata(
            url, Path(save_dir), headers=headers, cookies=cookies, **meta
        )
        if result.ok:
            self.events.post(
                Events.ARCHIVE_COMPLETED,
                {"ok": True, "path": result.path, "url": result.url, "title": result.title},
            )
        return result

    def close(self) -> None:
        pass
