"""In-app page → HLS/DASH manifest sniffing.

Given a *streaming-site page URL* (e.g. an episode page on hdtoday24 / yflix /
anikai) that a user pasted **directly** into the app, fetch the page HTML
in-process)Skip, scan it for an embedded ``.m3u8`` / ``.mpd`` manifest
reference, resolve any relative reference against the page URL, and prefer the
**player-host sibling** (```resolver prefers player-host over CDN-host``) so
the HLS engine can start immediately — without waiting for the browser
extension or a yt-dlp manifest re-scan.

This is the ``Media URL Detector`` stage of the streaming architecture, but run
*in-app* rather than in the extension. If the page referenced no manifest
(e.g. the player lives behind an authenticated iframe the plain HTTP fetch
cannot see), the call returns ``None`` and the manager falls through to its
existing yt-dlp + browser-assist paths exactly as before.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Awaitable
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

# A manifest reference is any ``.m3u8`` / ``.mpd`` token, optionally carrying a
# query string, appearing anywhere in the page (``<source src=…>``,
# ``<video src=…>``, ``data-src``, a JS config object ``file: "…"``, or an
# inline JSON blob).
_MANIFEST_RE = re.compile(
    r"""(?ix)
    (?:\.m3u8|\.mpd)            # extension
    (?:[?#]|["'\s]|$            # boundary: query / quote / whitespace / EOL
    )
    """
)

# Substrings that identify the *player host* (the CDN the extension was told
# to prefer when multiple manifest hosts show up for one page).
_PLAYER_HOST_HINTS = re.compile(
    r"player|megaplay|cdn\.play|manifest\.play|live\.play", re.IGNORECASE
)

# Schemes the resolver is willing to hand to the HLS engine.
_SAFE_SCHEMES = {"http", "https"}

_UNSAFE_SCHEMES = ("data:", "blob:", "javascript:", "about:", "vbscript:")

# Characters that terminate a manifest URL token when walking *left* from the
# matched ``.m3u8``/``.mpd``. Everything else (``/ : ? & = % # . - _ ~`` and
# alphanumerics) stays part of the reference, so absolute and protocol-relative
# URLs are captured whole instead of truncating at the last path segment.
_URL_TERMINATORS = frozenset('"\' \t\r\n<>=;,([{&`')

_RedirectFetcher = Callable[[str], Awaitable[tuple[str, str]]]


def _to_absolute(url: str, page_url: str) -> str | None:
    """Resolve *url* (possibly relative) against *page_url*; return ``None``
    for blob:/data:/javascript: or anything outside http(s)."""
    if not url or not page_url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if candidate.lower().startswith(_UNSAFE_SCHEMES):
        return None
    joined = urljoin(page_url, candidate)
    parsed = urlparse(joined)
    if parsed.scheme not in _SAFE_SCHEMES or not parsed.hostname:
        return None
    return joined


def find_manifest_urls(html: str, page_url: str) -> list[str]:
    """Return every absolute manifest URL referenced by *html*, in document
    order, with relative references resolved against *page_url*."""
    out: list[str] = []
    for match in _MANIFEST_RE.finditer(html):
        start = match.start()
        # walk back to the start of the reference token; the regex consumes the
        # trailing delimiter (a quote/space), so the leading walk must tolerate
        # every URL character including ``/ : ? & =``.
        while start > 0 and html[start - 1] not in _URL_TERMINATORS:
            start -= 1
        end = match.end()
        while end < len(html) and html[end] not in ('"', "'", " ", "<", ">", "\n", "\t"):
            end += 1
        raw = html[start:end].strip().strip("\"'")
        if not raw:
            continue
        absolute = _to_absolute(raw, page_url)
        if absolute is not None and absolute not in out:
            out.append(absolute)
    return out


def prefer_player_host(candidates: list[str]) -> str | None:
    """Return the first candidate whose host hints it is a *player* host,
    falling back to the first candidate overall. Mirrors the manager's
    ``_prefer_player_host_sibling`` rule used when the extension hands over
    several manifest URLs for one page."""
    if not candidates:
        return None
    for candidate in candidates:
        host = (urlparse(candidate).hostname or "").lower()
        if _PLAYER_HOST_HINTS.search(host):
            return candidate
    return candidates[0]


@dataclass
class SniffResult:
    """The manifest + frame context a successful page sniff produced."""

    manifest_url: str
    page_url: str
    frame_url: str | None = None
    referer: str | None = None
    candidates: list[str] = field(default_factory=list)

    def to_context_kwargs(self) -> dict:
        return {
            "page_url": self.page_url,
            "frame_url": self.frame_url,
            "referer": self.referer,
        }


class PageSniffError(Exception):
    """Transport-level failure while fetching *page_url* (DNS/connection/
    timeout). Callers can distinguish this from *no manifest found*."""


async def fetch_page_text(
    page_url: str,
    *,
    fetch: _RedirectFetcher,
) -> str:
    """Fetch *page_url* through the injected *fetch* callable (expected to
    honour redirects and return ``(status_or_empty, body_text)``); raise
    :class:`PageSniffError` on transport failure."""
    try:
        status, body = await fetch(page_url)
    except Exception as exc:  # noqa: BLE001 - transport-level
        raise PageSniffError(f"page fetch failed: {exc}") from exc
    if not body:
        raise PageSniffError("page returned no body")
    return body


async def sniff_page_manifest(
    page_url: str,
    *,
    fetch: _RedirectFetcher,
    html: str | None = None,
) -> SniffResult | None:
    """Fetch *page_url*, locate an embedded manifest, and return a
    :class:`SniffResult` with the best (player-host) candidate.

    Returns ``None`` when the page carries no manifest reference. Use
    :func:`find_manifest_urls` + :func:`prefer_player_host` for a pure,
    network-free scan of already-fetched HTML.
    """
    if html is None:
        html = await fetch_page_text(page_url, fetch=fetch)
    candidates = find_manifest_urls(html, page_url)
    if not candidates:
        return None
    chosen = prefer_player_host(candidates)
    if chosen is None:
        return None
    return SniffResult(
        manifest_url=chosen,
        page_url=page_url,
        frame_url=page_url,
        referer=page_url,
        candidates=candidates,
    )
