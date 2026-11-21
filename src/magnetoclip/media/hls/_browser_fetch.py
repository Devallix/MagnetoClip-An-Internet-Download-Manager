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
    frame_url: str | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    context=None,
    timeout: float = 90.0,
) -> str:
    from ..browser_streaming import BrowserFetcher

    request_headers = dict(headers or {})
    request_cookies = dict(cookies or {})
    if context is not None:
        try:
            captured_headers = context.fetch_headers()
        except Exception:  # noqa: BLE001 - old serialized contexts may be partial
            captured_headers = dict(getattr(context, "headers", {}) or {})
        request_headers = {**captured_headers, **request_headers}
        request_cookies = {**dict(getattr(context, "cookies", {}) or {}), **request_cookies}
        referer = referer or getattr(context, "referer", None)
        frame_url = frame_url or getattr(context, "frame_url", None)
    fetcher = BrowserFetcher(
        session_factory,
        referer=referer,
        frame_url=frame_url,
        headers=request_headers,
        cookies=request_cookies,
        cancel_event=threading.Event(),
        timeout=timeout,
    )
    try:
        return fetcher.fetch_text(url)
    finally:
        fetcher.close()