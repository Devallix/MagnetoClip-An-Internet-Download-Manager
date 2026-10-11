"""Curated catalog of sites offered on the Download Sites page.

The data mirrors the reference notes kept in ``sites_info/`` at the repo root:

* ``Torrent Sites.txt``   -> :data:`TORRENT_SITES`
* ``Anime Sites.txt``     -> :data:`ANIME_SITES`
* ``Streaming Sites.txt`` -> :data:`STREAMING_SITES`

The notes themselves are research material rather than shipped resources (they
are not bundled by ``scripts/magnetoclip.spec``), so the tables are embedded
here. The streaming table was extracted from the ``Streaming Sites.txt`` link
(https://www.bestfreestreaming.org/, table "The best sites to watch free
movies and tv shows (2026)", last updated 25 Sep 2026) and its columns are
folded into the description column used by the UI.

Entries carry the site's primary url. Where the notes list mirrors or proxies,
only the primary domain is kept here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Every site in the catalog is reachable without an account or payment.
FREE = "Free"


@dataclass(frozen=True)
class SiteEntry:
    """One row of a site table."""

    name: str
    description: str
    url: str
    usage: str = FREE


@dataclass(frozen=True)
class SiteCategory:
    """A tab on the Download Sites page."""

    key: str
    label: str
    entries: tuple[SiteEntry, ...]


_TORRENTS: tuple[SiteEntry, ...] = (
    SiteEntry(
        "The Pirate Bay",
        "Has one of the largest torrent libraries on the internet and works in "
        "most countries. Its standout feature is the trusted uploader system: "
        "uploaders with a skull icon are usually reliable, and torrents "
        "regularly come with thousands of seeders for very fast downloads.",
        "https://thepiratebay.org/",
    ),
    SiteEntry(
        "1337x",
        "Great for movies and TV. One of the most popular torrent sites in "
        "2026, and noticeably cleaner and better organized than older "
        "trackers. The trending section is genuinely useful, and the category "
        "filters make it easy to browse documentaries, anime, movies and TV "
        "series.",
        "https://1337x.to/",
    ),
    SiteEntry(
        "YTS",
        "Best for HD movies. Specializes in high-quality movie torrents with "
        "much smaller file sizes than other sites, so 1080p and 4K downloads "
        "keep an excellent quality-to-size ratio on limited storage or slower "
        "internet.",
        "https://web.yts.gg/",
    ),
    SiteEntry(
        "EZTV",
        "Best for TV shows. New episodes usually appear within hours of "
        "airing, it loads fast, and it carries fewer annoying ads than many "
        "other torrent sites.",
        "https://en.eztv-official.is/",
    ),
    SiteEntry(
        "TorrentDownloads",
        "Good for rare content. Surprisingly useful for finding obscure or "
        "older files such as rare books, niche software and less popular "
        "games. It does show more ads than the others, but most are limited "
        "to the sidebars and do not interrupt browsing.",
        "https://torrentdownloads.pro/",
    ),
    SiteEntry(
        "LimeTorrents",
        "Good for new releases. One of the most beginner-friendly torrent "
        "sites, and the health bar next to each torrent gives a quick visual "
        "indicator of speed and reliability.",
        "https://limetorrents.info/",
    ),
    SiteEntry(
        "Nyaa",
        "Best for anime. Unbeatable for anime series, movies and manga, with "
        "a massive library backed by strong community support.",
        "https://nyaa.si/",
    ),
    SiteEntry(
        "Internet Archive",
        "Best legal option. For a completely legal torrenting experience, this "
        "is the pick, offering public-domain movies, books, music and "
        "software.",
        "https://archive.org/",
    ),
    SiteEntry(
        "FitGirl Repacks",
        "Best for gamers. A top site for PC games, and the compression is "
        "incredible: large games download much faster, saving bandwidth and "
        "storage.",
        "https://fitgirl-repacks.site/",
    ),
    SiteEntry(
        "TorrentGalaxy",
        "One of the best torrent sites in 2026, especially for browsing movies "
        "and new releases visually. Unlike older sites that only show text "
        "links, it uses posters and preview images, which makes discovering "
        "new content much easier.",
        "https://torrentgalaxy-official.is/",
    ),
)

_ANIME: tuple[SiteEntry, ...] = (
    SiteEntry(
        "SitesHiAnime",
        "Offers a large library of subbed and dubbed series in high definition "
        "with minimal interruptions. (Recommended)",
        "https://hianimes.se/",
    ),
    SiteEntry(
        "Animepahe",
        "Known for hosting compressed video files that load quickly and save "
        "data while maintaining good video quality.",
        "https://animepahe.ch/",
    ),
    SiteEntry(
        "Anitaku",
        "A popular platform with a familiar layout and frequently updated "
        "episode listings.",
        "https://anitaku.com.ro/",
    ),
    SiteEntry(
        "Miruro",
        "A clean, multi-source streaming alternative frequently recommended by "
        "anime tracking communities.",
        "https://www.miruro.com/",
    ),
    SiteEntry(
        "EverythingMoe",
        "Acts as a comprehensive directory index listing active "
        "community-favorite anime and manga sites.",
        "https://everythingmoe.com/",
    ),
)

_STREAMING: tuple[SiteEntry, ...] = (
    SiteEntry("BingeBang", "Movies and TV shows. Few ads — use an ad-blocker.", "https://bingebang.club/"),
    SiteEntry("Reelix", "Movies and TV shows. Few ads — use an ad-blocker.", "https://reelix.ac"),
    SiteEntry("FlyStream", "Movies and TV shows. Few ads — use an ad-blocker.", "https://flystream.net"),
    SiteEntry("Zoechip", "Movies and TV shows. Few ads — use an ad-blocker.", "https://zoechip.bz/home"),
    SiteEntry("Atlantic", "Movies and TV shows. Few ads — use an ad-blocker.", "https://atlantic.st"),
    SiteEntry("MovieNerds", "Movies and TV shows. Few ads — use an ad-blocker.", "https://movienerds.site"),
    SiteEntry("Zenox", "Movies and TV shows. Few ads — use an ad-blocker.", "https://zenox.lol"),
    SiteEntry("AllFlix", "Movies and TV shows. Few ads — use an ad-blocker.", "https://allflix.org"),
    SiteEntry("Flickbuff", "Movies and TV shows. Few ads — use an ad-blocker.", "https://flickbuff.cc"),
    SiteEntry("Cineapse", "Movies and TV shows. Few ads — use an ad-blocker.", "https://cineapse.net"),
    SiteEntry("Shiopa", "Movies and TV shows. Few ads — use an ad-blocker.", "https://shiopa.com"),
    SiteEntry("P-Stream", "Movies and TV shows. Few ads — use an ad-blocker.", "https://pstream.cfd"),
    SiteEntry("Nova", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://novahd.cc"),
    SiteEntry("Cinejoy", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://cinejoy.to"),
    SiteEntry("ZetMoon", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://zetmoon.live"),
    SiteEntry("Flaxfer", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://flaxfer.lol"),
    SiteEntry("CineHD", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://cinehd.vc/home"),
    SiteEntry("stellar", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://stellar.gdn"),
    SiteEntry("Filmex", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://filmex.to"),
    SiteEntry("Flixeo", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://flixeo.tv/home"),
    SiteEntry("NightFlix", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://nightflix.us"),
    SiteEntry("VidPlay", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://vidplay.top"),
    SiteEntry("HydraHD", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://hydrahd.info"),
    SiteEntry("Rive", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://rentry.co/rivestream"),
    SiteEntry("Redflix", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://redflix.one"),
    SiteEntry("HiMovies", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://himovies.ac/home"),
    SiteEntry("RidoMovies", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://ridomovies.tv/home"),
    SiteEntry("Flixbaba", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://flixbaba.info"),
    SiteEntry("StreamingUnity", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://streamingunity.dog"),
    SiteEntry("MoviePlex", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://movieplex.online"),
    SiteEntry("VidFlix", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://vidflix.live"),
    SiteEntry("TheFlixBay", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://theflixbay.com"),
    SiteEntry("NEPU", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://nepu.io"),
    SiteEntry("NetPrime", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://netprime.bz"),
    SiteEntry("bCine", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://bcine.ru"),
    SiteEntry("SolarMovie", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://solarmovie2.com"),
    SiteEntry("YesMovies", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://yesmovies.ag"),
    SiteEntry("MyFlixer", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://myflixer.sc/home"),
    SiteEntry("2Flix", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://2flix.lol/home"),
    SiteEntry("MoviesJoy", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://moviesjoy.bz/home"),
    SiteEntry("CornClick", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://cornclick.com"),
    SiteEntry("Flixer", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://flixer.gd"),
    SiteEntry("MyFlixerz", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://myflixerz.org/home"),
    SiteEntry("HDToday", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://hdtoday.sc/home"),
    SiteEntry("Movish", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://movish.to/home"),
    SiteEntry("PressPlayz", "Movies and TV shows. Moderate ads — use an ad-blocker.", "https://pressplayz.to/home"),
    SiteEntry("SeeFlix", "Movies and TV shows. Intrusive ads — use an ad-blocker.", "https://seeflix.to/home"),
    SiteEntry("BoredFlix", "Movies and TV shows. Intrusive ads — use an ad-blocker.", "https://boredflix.to"),
    SiteEntry("NOXX", "TV shows only. Intrusive ads — use an ad-blocker.", "https://noxx.gg"),
    SiteEntry("AZMovies", "Movies only. Intrusive ads — use an ad-blocker.", "https://azmovies.to"),
)

TORRENT_SITES = SiteCategory(
    key="torrents",
    label="Torrents Sites",
    entries=_TORRENTS,
)

ANIME_SITES = SiteCategory(
    key="anime",
    label="Anime Sites",
    entries=_ANIME,
)

STREAMING_SITES = SiteCategory(
    key="streaming",
    label="Streaming Sites",
    entries=_STREAMING,
)

#: Tab order shown on the page.
SITE_CATEGORIES: tuple[SiteCategory, ...] = (
    TORRENT_SITES,
    ANIME_SITES,
    STREAMING_SITES,
)


def sites_by_key(key: str) -> SiteCategory | None:
    """Return the category with *key*, or ``None`` when there is no match."""
    for category in SITE_CATEGORIES:
        if category.key == key:
            return category
    return None


__all__ = [
    "ANIME_SITES",
    "FREE",
    "SITE_CATEGORIES",
    "STREAMING_SITES",
    "TORRENT_SITES",
    "SiteCategory",
    "SiteEntry",
    "sites_by_key",
]
