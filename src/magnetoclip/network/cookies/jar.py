"""Cookie jar: parse, merge, and serialize HTTP cookie headers."""

from __future__ import annotations

from dataclasses import dataclass, field


def parse_cookie_header(header: str) -> dict[str, str]:
    """Parse a ``Cookie`` header value into ``{name: value}``."""
    cookies: dict[str, str] = {}
    if not header:
        return cookies
    for pair in header.split(";"):
        if "=" not in pair:
            continue
        name, _, value = pair.strip().partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def format_cookie_header(cookies: dict[str, str]) -> str:
    """Serialize ``{name: value}`` into a ``Cookie`` header value."""
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


@dataclass
class CookieJar:
    """A small persistent cookie store, serializable to JSON."""

    cookies: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, value: str) -> None:
        if name and value is not None:
            self.cookies[name] = value

    def merge(self, other: "CookieJar | dict[str, str] | None") -> None:
        if other is None:
            return
        if isinstance(other, CookieJar):
            other = other.cookies
        for name, value in other.items():
            self.add(name, value)

    def parse_header(self, header: str | None) -> None:
        self.merge(parse_cookie_header(header or ""))

    def to_header(self) -> str:
        return format_cookie_header(self.cookies)

    def to_dict(self) -> dict[str, str]:
        return dict(self.cookies)

    @classmethod
    def from_dict(cls, data: dict[str, str] | None) -> "CookieJar":
        return cls(cookies=dict(data or {}))

    @classmethod
    def from_header(cls, header: str | None) -> "CookieJar":
        return cls(cookies=parse_cookie_header(header or ""))
