"""Cookie helper tests."""

from __future__ import annotations

from magnetoclip.network.cookies.jar import (
    CookieJar,
    format_cookie_header,
    parse_cookie_header,
)


def test_parse_single_and_multi():
    assert parse_cookie_header("a=1; b=2") == {"a": "1", "b": "2"}
    assert parse_cookie_header("") == {}
    assert parse_cookie_header("bare_no_equals; a=1") == {"a": "1"}


def test_format_round_trip():
    raw = format_cookie_header({"session": "abc", "theme": "dark"})
    assert "session=abc" in raw
    assert "theme=dark" in raw
    assert parse_cookie_header(raw) == {"session": "abc", "theme": "dark"}


def test_jar_merge_and_header():
    jar = CookieJar({"a": "1"})
    jar.add("b", "2")
    jar.merge({"c": "3"})
    assert jar.to_dict() == {"a": "1", "b": "2", "c": "3"}
    assert jar.to_header() == "a=1; b=2; c=3"


def test_jar_from_header_and_dict():
    assert CookieJar.from_header("a=1").to_dict() == {"a": "1"}
    assert CookieJar.from_dict({"x": "y"}).to_dict() == {"x": "y"}
    assert CookieJar.from_dict(None).to_dict() == {}


def test_jar_skips_empty_names():
    jar = CookieJar()
    jar.add("", "v")
    jar.add("ok", "")
    assert jar.to_dict() == {"ok": ""}
