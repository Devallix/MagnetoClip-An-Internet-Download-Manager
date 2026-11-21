"""Site configuration for the built-in torrent search engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TorrentSite:
    """Definition of a searchable torrent site."""

    key: str
    name: str
    base_url: str
    search_url: str
    result_type: str  # "json_api" or "html"
    enabled: bool = True


SITES: dict[str, TorrentSite] = {
    "yts": TorrentSite(
        key="yts",
        name="YTS",
        base_url="https://yts.mx",
        search_url="https://yts.mx/api/v2/list_movies.json?query_term={query}&limit=20",
        result_type="json_api",
    ),
    "yts_torrench": TorrentSite(
        key="yts_torrench",
        name="YTS (Torrench)",
        base_url="https://yts.torrench.cf",
        search_url="https://yts.torrench.cf/api/v2/list_movies.json?query_term={query}&limit=20",
        result_type="json_api",
    ),
}


def get_site(key: str) -> TorrentSite | None:
    return SITES.get(key)


def enabled_sites() -> list[TorrentSite]:
    return [s for s in SITES.values() if s.enabled]


def all_site_keys() -> list[str]:
    return list(SITES.keys())
