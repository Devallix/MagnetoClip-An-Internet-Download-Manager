"""Torrent site search engine — fetches search results from configured sites."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from magnetoclip.services.logging.setup import get_logger

from .sites import get_site, enabled_sites

log = get_logger(__name__)

_SEARCH_TIMEOUT = 15.0


@dataclass
class TorrentResult:
    """A single search result from a torrent site."""

    title: str
    size: int
    size_text: str
    seeds: int
    leechers: int
    magnet_uri: str | None = None
    torrent_url: str | None = None
    poster: str | None = None
    quality: str | None = None
    rating: str | None = None
    year: str | None = None
    site: str = ""
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "size": self.size,
            "size_text": self.size_text,
            "seeds": self.seeds,
            "leechers": self.leechers,
            "magnet_uri": self.magnet_uri,
            "torrent_url": self.torrent_url,
            "poster": self.poster,
            "quality": self.quality,
            "rating": self.rating,
            "year": self.year,
            "site": self.site,
            "url": self.url,
        }


def _parse_size_bytes(size_str: str) -> int:
    """Parse a human-readable size string to bytes."""
    match = re.match(r"([\d.]+)\s*(B|KB|MB|GB|TB)", size_str, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * multipliers.get(unit, 1))


def _parse_yts_json(data: dict, site_key: str) -> list[TorrentResult]:
    """Parse YTS API JSON response into TorrentResult list."""
    results = []
    movies = data.get("data", {}).get("movies") or []
    for movie in movies:
        title = movie.get("title_long") or movie.get("title", "Unknown")
        poster = movie.get("medium_cover_image") or movie.get("background_image")
        rating = str(movie.get("rating", ""))
        year = str(movie.get("year", ""))

        for torrent in movie.get("torrents") or []:
            quality = torrent.get("quality", "")
            type_str = torrent.get("type", "")
            size = torrent.get("size", "0 B")
            magnet = torrent.get("magnet") or torrent.get("url")
            hash_val = torrent.get("hash", "")
            if hash_val and not magnet:
                magnet = f"magnet:?xt=urn:btih:{hash_val}"

            torrent_title = f"{title} [{quality}]"
            if type_str:
                torrent_title += f" ({type_str})"

            results.append(
                TorrentResult(
                    title=torrent_title,
                    size=_parse_size_bytes(size),
                    size_text=size,
                    seeds=movie.get("peers", 0),
                    leechers=movie.get("like_count", 0),
                    magnet_uri=magnet,
                    poster=poster,
                    quality=quality,
                    rating=rating,
                    year=year,
                    site=site_key,
                    url=movie.get("url"),
                )
            )
    return results


async def search_torrents(
    query: str,
    site_key: str | None = None,
    timeout: float = _SEARCH_TIMEOUT,
) -> list[TorrentResult]:
    """Search for torrents across configured sites.

    If *site_key* is None, searches all enabled sites and merges results.
    """
    sites = []
    if site_key:
        site = get_site(site_key)
        if site:
            sites = [site]
    else:
        sites = enabled_sites()

    if not sites:
        return []

    all_results: list[TorrentResult] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for site in sites:
            try:
                url = site.search_url.format(query=query)
                response = await client.get(url)
                response.raise_for_status()

                if site.result_type == "json_api":
                    data = response.json()
                    if site.key in ("yts", "yts_torrench"):
                        all_results.extend(_parse_yts_json(data, site.key))
                elif site.result_type == "html":
                    # HTML parsing can be added for other sites
                    pass
            except Exception as exc:
                log.warning(
                    "torrent_search_failed",
                    site=site.key,
                    query=query,
                    error=str(exc),
                )
                continue

    return all_results
