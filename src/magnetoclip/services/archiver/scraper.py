"""PageScraper: fetch a webpage and inline its resources as base64.

Implemented with the standard library ``html.parser`` (no external dependency)
so it works in any environment. For each supported tag/attribute it resolves
the resource URL, downloads it (honouring max size), and rewrites the attribute
to a ``data:`` URI when inlining is enabled.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

# (tag, attribute) pairs whose URL is a page resource we may inline.
_RESOURCE_ATTRS = {
    ("img", "src"),
    ("img", "srcset"),
    ("script", "src"),
    ("link", "href"),  # css / icons
    ("input", "src"),
    ("source", "src"),
    ("source", "srcset"),
    ("video", "src"),
    ("audio", "src"),
    ("iframe", "src"),
    ("embed", "src"),
    ("object", "data"),
    ("image", "href"),
}

_STYLESHEET_ATTRS = {("link", "href")}
_SCRIPT_ATTRS = {("script", "src")}
_IMAGE_ATTRS = {
    ("img", "src"),
    ("img", "srcset"),
    ("input", "src"),
    ("source", "src"),
    ("video", "src"),
    ("audio", "src"),
    ("image", "href"),
}


class PageScraper:
    def __init__(self, context) -> None:
        self.context = context

    def _enabled(self, key: str) -> bool:
        return bool(self.context.settings.get(f"archiver.{key}", True))

    def _max_resource_bytes(self) -> int:
        return int(self.context.settings.get("archiver.max_resource_size", 5)) * 1024 * 1024

    def _timeout(self) -> int:
        return int(self.context.settings.get("archiver.timeout", 30))

    _DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    def _client(self, headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> httpx.Client:
        """An httpx client with a browser-like header set.

        ``headers``/``cookies`` carry session state captured from the user's
        browser (e.g. the ``Cookie`` header for session-gated pages); they are
        merged over the defaults so session-gated fetches (which otherwise 403)
        behave like the real page view.
        """
        timeout = self._timeout()
        extra = dict(headers or {})
        if cookies:
            from ...network.cookies.jar import format_cookie_header

            all_cookies: dict[str, str] = dict(cookies)
            existing = extra.pop("cookie", None)
            if existing:
                all_cookies.update(
                    dict(
                        pair.split("=", 1)
                        for pair in existing.split(";")
                        if "=" in pair
                    )
                )
            extra["cookie"] = format_cookie_header(all_cookies)
        merged = {
            **self._DEFAULT_HEADERS,
            **{key: str(value) for key, value in extra.items()},
        }
        return httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=merged,
        )

    # ----- main flow -----

    def fetch_html(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Fetch *url* and return ``(html_text, base_url)``."""
        with self._client(headers, cookies) as client:
            response = client.get(url)
            response.raise_for_status()
        base_url = str(response.url)
        return response.text, base_url

    def inline(
        self,
        html: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Rewrite *html*, inlining resources as data URIs.

        Returns a dict with ``html`` (rewritten), ``resources`` (list of
        ArchiveResource), and counts.
        """
        inline_images = self._enabled("inline_images")
        inline_css = self._enabled("inline_css")
        inline_js = self._enabled("inline_js")

        replacements: dict[str, str] = {}  # original url -> data uri (if inlined)
        resources: list[Any] = []
        inlined = 0
        failed = 0

        max_bytes = self._max_resource_bytes()
        seen: set[str] = set()

        for (tag, attr, token) in self._iter_tokens(html):
            url = _extract_url(token, tag, attr)
            if not url or url in seen:
                continue
            seen.add(url)
            absolute = urljoin(base_url, url)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            should_inline = (
                (tag, attr) in _IMAGE_ATTRS and inline_images
            ) or ((tag, attr) in _STYLESHEET_ATTRS and inline_css and _is_css(absolute)) or (
                (tag, attr) in _SCRIPT_ATTRS and inline_js
            )
            if not should_inline:
                continue
            try:
                data, mime = self._fetch_resource(
                    absolute, max_bytes, (tag, attr), headers=headers, cookies=cookies
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                resources.append({"tag": tag, "attr": attr, "url": absolute, "error": str(exc)})
                continue
            if data is None:
                failed += 1
                resources.append({"tag": tag, "attr": attr, "url": absolute, "error": "too large/unsupported"})
                continue
            data_uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            replacements[url] = data_uri
            inlined += 1
            resources.append({"tag": tag, "attr": attr, "url": absolute, "data_base64": data_uri, "inline": True})

        # Rewrite the document, replacing original (relative) urls with data URIs.
        rewritten = _rewrite_uris(html, replacements)

        return {
            "html": rewritten,
            "resources": resources,
            "resources_inlined": inlined,
            "resources_failed": failed,
        }

    def _fetch_resource(
        self,
        url: str,
        max_bytes: int,
        kind: tuple[str, str],
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> tuple[bytes | None, str]:
        with self._client(headers, cookies) as client, client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            chunks = []
            total = 0
            for chunk in response.iter_bytes(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    return None, content_type or "application/octet-stream"
                chunks.append(chunk)
            data = b"".join(chunks)
        mime = content_type or _guess_mime(url)
        if not mime:
            mime = _guess_mime(url)
        return data, mime or "application/octet-stream"

    def _iter_tokens(self, html: str):
        # Walk the document and yield (tag, attr, full_token) protecting attr urls.
        for match in re.finditer(
            r"<(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\b(?P<body>[^>]*?)(?P<selfclose>/?)>",
            html,
        ):
            tag = match.group("tag").lower()
            body = match.group("body")
            for attr_match in re.finditer(
                r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
                body,
            ):
                attr = attr_match.group(1).lower()
                value = (
                    attr_match.group(3)
                    or attr_match.group(4)
                    or attr_match.group(5)
                )
                if (tag, attr) in _RESOURCE_ATTRS and value:
                    yield (tag, attr, match.group(0))
                    break  # only the first resource attr per token to keep simple


def _extract_url(token: str, tag: str, attr: str) -> str:
    attr_re = re.compile(
        rf"{attr}\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
        re.IGNORECASE,
    )
    m = attr_re.search(token)
    if not m:
        return ""
    raw = m.group(1)
    if raw[:1] in ("\"", "'"):
        raw = raw[1:-1]
    return raw.strip()


def _is_css(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".css") or "stylesheet" in url


def _guess_mime(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".gif"):
        return "image/gif"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".svg"):
        return "image/svg+xml"
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".js"):
        return "application/javascript"
    if path.endswith(".woff2"):
        return "font/woff2"
    return "application/octet-stream"


def _rewrite_uris(html: str, replacements: dict[str, str]) -> str:
    """Replace original resource URLs with data URIs inside attribute values."""
    if not replacements:
        return html

    def _repl(match) -> str:
        attr = match.group(1)
        quote = match.group(2)
        value = match.group(3)
        if value in replacements:
            return f"{attr}={quote}{replacements[value]}{quote}"
        return match.group(0)

    pattern = re.compile(r"([a-zA-Z:_-]+)\s*=\s*(\")([^\"]*)(\")")
    pattern2 = re.compile(r"([a-zA-Z:_-]+)\s*=\s*(')([^']*)(')")

    def _apply(regex, text):
        def _run(m) -> str:
            attr = m.group(1)
            quote = m.group(2)
            value = m.group(3)
            if value in replacements:
                return f"{attr}={quote}{replacements[value]}{quote}"
            return m.group(0)

        return regex.sub(_run, text)

    result = _apply(pattern, html)
    result = _apply(pattern2, result)
    return result
