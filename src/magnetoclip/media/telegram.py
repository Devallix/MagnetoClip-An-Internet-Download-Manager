"""Recognition of Telegram Web session-bound media proxy URLs.

Telegram Web (K and A) plays media through same-origin proxies such as
``/k/stream/``, ``/a/stream/``, ``/a/progressive/`` and ``/api/files/`` that the
browser implicitly fires with the page's session cookies. The proxy answers with
a 302 to a signed CDN file, but only when the request carries those cookies —
so a standalone client (MagnetoClip) that fetches the raw URL always gets a 302
(redirect loop / login) instead of the file.

These URLs are therefore only downloadable from inside the open Telegram Web tab,
where the browser-injected extension can ``fetch()`` them with credentials. Helpers
here let the app recognise them and route them through the browser.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Path prefixes on web.telegram.org / telegram.org that proxy session-bound media.
_PROXY_PATH_PREFIXES = (
    "/k/stream/",
    "/a/stream/",
    "/a/progressive/",
    "/api/files/",
)


def is_telegram_web_media_url(url: str) -> bool:
    """True when *url* is a Telegram Web session-bound media proxy link.

    These cannot be downloaded directly by MagnetoClip (they need the page's
    session cookie) and must be fetched from the open browser tab instead.
    """
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host != "web.telegram.org" and not host.endswith(".web.telegram.org"):
        return False
    path = parsed.path or ""
    return path.startswith(_PROXY_PATH_PREFIXES)
