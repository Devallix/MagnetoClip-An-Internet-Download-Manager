from __future__ import annotations

from types import SimpleNamespace

from magnetoclip.services.archiver.scraper import (
    PageScraper,
    _extract_url,
    _rewrite_uris,
)


def _scraper() -> PageScraper:
    return PageScraper(
        SimpleNamespace(
            settings={
                "archiver.inline_images": False,
                "archiver.inline_css": False,
                "archiver.inline_js": False,
                "archiver.max_resource_size": 5,
                "archiver.timeout": 30,
            }
        )
    )


def test_inline_does_not_crash_on_real_html():
    # Regression: feed() previously exploded with
    # "'_ExtractParser' object has no attribute '_handle'" because the parser
    # called an undefined method while scanning every tag.
    html = (
        '<img src="a.png"><link rel="stylesheet" href="style.css">'
        '<script src="app.js"></script><h1>hi</h1>'
    )
    result = _scraper().inline(html, "https://example.com/page")
    assert "html" in result
    assert isinstance(result["resources"], list)
    assert result["resources_failed"] == 0
    assert result["html"] == html


def test_rewrite_uris_inlines_both_attrs():
    html = '<img src="a.png" alt="x"><link rel="stylesheet" href="style.css">'
    repl = {
        "a.png": "data:image/png;base64,AAA",
        "style.css": "data:text/css;base64,BBB",
    }
    out = _rewrite_uris(html, repl)
    assert "data:image/png;base64,AAA" in out
    assert "data:text/css;base64,BBB" in out
    assert "a.png" not in out


def test_rewrite_uris_noop_when_no_match():
    html = '<img src="a.png">'
    assert _rewrite_uris(html, {}) == html


def test_extract_url():
    assert _extract_url('<img src="a.png">', "img", "src") == "a.png"
    assert _extract_url('<a href="page.html">x</a>', "a", "href") == "page.html"


def test_extract_url_missing_returns_none():
    assert _extract_url("<img alt='x'>", "img", "src") in ("", None)
