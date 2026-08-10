from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROXY_TYPES = ("direct", "http", "https", "socks5")


@dataclass
class ProxySpec:
    """A proxy profile. ``type`` is one of ``direct/http/https/socks5``."""

    type: str = "direct"
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if self.type not in PROXY_TYPES:
            raise ValueError(f"unsupported proxy type: {self.type}")

    def to_url(self) -> str | None:
        """Render an http(s) proxy URL for httpx."""
        if self.type not in ("http", "https") or not self.host:
            return None
        scheme = "https" if self.type == "https" else "http"
        credentials = ""
        if self.username:
            credentials = f"{self.username}:{self.password or ''}@"
        port = f":{self.port}" if self.port else ""
        return f"{scheme}://{credentials}{self.host}{port}"

    @classmethod
    def from_db_row(cls, row: Any) -> ProxySpec:
        """Build a spec from a ``proxy_profiles`` DB row (or dict-like)."""
        return cls(
            type=getattr(row, "type", "direct") or "direct",
            host=getattr(row, "host", None),
            port=getattr(row, "port", None),
            username=getattr(row, "username_ref", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ProxySpec:
        data = data or {}
        return cls(
            type=data.get("type", "direct"),
            host=data.get("host"),
            port=data.get("port"),
            username=data.get("username"),
            password=data.get("password"),
        )
