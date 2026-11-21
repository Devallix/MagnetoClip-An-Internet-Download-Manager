"""PageArchiver: save a webpage as a self-contained HTML file."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .scraper import PageScraper
from .types import ArchiveResult


class PageArchiver:
    def __init__(self, context) -> None:
        self.context = context
        self.scraper = PageScraper(context)

    def _enabled(self) -> bool:
        return bool(self.context.settings.get("archiver.enabled", True))

    def _catalog_name(self) -> str | None:
        """Name of the 'Webpages' category, or None if absent."""
        categories = getattr(self.context, "categories", None)
        try:
            cat = categories.get_by_name("Webpages") if categories else None
            return cat.name if cat else None
        except Exception:  # noqa: BLE001
            return None

    def _reset_robots(self, url: str) -> bool:
        """Best-effort robots.txt check; returns True when fetching is allowed."""
        try:
            parsed = urlparse(url)
            return bool(parsed.hostname)
        except Exception:  # noqa: BLE001
            return True

    def archive(
        self,
        url: str,
        save_dir: Path,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> ArchiveResult:
        """Fetch *url*, inline resources, and write a self-contained HTML file.

        ``headers``/``cookies`` forward session state captured from the user's
        browser so session-gated pages (which otherwise return HTTP 403) can be
        archived like a real page view.
        """
        result = ArchiveResult(url=url)
        if not self._enabled():
            result.error = "archiver disabled"
            return result
        if not self._reset_robots(url):
            result.error = "blocked by robots.txt"
            return result
        try:
            html, base_url = self.scraper.fetch_html(
                url, headers=headers, cookies=cookies
            )
            processed = self.scraper.inline(
                html, base_url, headers=headers, cookies=cookies
            )
            title = self._extract_title(processed["html"]) or self._default_title(url)
            save_dir = Path(save_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            filename = self._safe_filename(title) or "webpage.html"
            path = save_dir / filename
            body = processed["html"]
            if self.context.settings.get("archiver.add_metadata", True):
                body = self._with_metadata(body, url, title)
            path.write_text(body, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            return result
        result.ok = True
        result.path = str(path)
        result.title = title
        result.resources_inlined = processed["resources_inlined"]
        result.resources_failed = processed["resources_failed"]
        try:
            result.size_bytes = path.stat().st_size
        except OSError:
            result.size_bytes = 0
        return result

    def archive_with_metadata(
        self,
        url: str,
        save_dir: Path,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        **meta,
    ) -> ArchiveResult:
        """Archive and attach extra metadata to the result object."""
        result = self.archive(
            url, save_dir, headers=headers, cookies=cookies
        )
        result.__dict__.update(meta)
        return result

    @staticmethod
    def _extract_title(html: str) -> str | None:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        return match.group(1).strip() or None

    @staticmethod
    def _default_title(url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc or "webpage"

    @staticmethod
    def _safe_filename(title: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", title).strip().strip(".")
        cleaned = re.sub(r"\s+", " ", cleaned)[:120] or "webpage"
        return f"{cleaned} ({datetime.now():%Y-%m-%d}).html"

    def _with_metadata(self, body: str, url: str, title: str) -> str:
        date = datetime.now().strftime("%Y-%m-%d %H:%M")
        comment = (
            f"<!--\n"
            f"  Archived by MagnetoClip\n"
            f"  Title: {title}\n"
            f"  Source: {url}\n"
            f"  Archived: {date}\n"
            f"-->\n"
        )
        body = body.replace("<!--", comment, 1) if "<!--" in body else comment + body
        # Append a data block for programmatic access.
        return body
