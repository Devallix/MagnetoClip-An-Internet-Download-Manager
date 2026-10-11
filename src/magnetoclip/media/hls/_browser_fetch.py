"""Browser-extension fetch transport (thin).

Keep the extension native-messaging round trip out of the analyzer module so
analysis logic stays importable without the transport being connected.
"""

from __future__ import annotations

import threading


def browser_fetch_text(
    session_factory,
    url: str,
    *,
    referer: str | None = None,
    timeout: float = 90.0,
) -> str:
    from ..browser_streaming import BrowserFetcher

    fetcher = BrowserFetcher(
        session_factory,
        referer=referer,
        cancel_event=threading.Event(),
        timeout=timeout,
    )
    try:
        return fetcher.fetch_text(url)
    finally:
        fetcher.close()