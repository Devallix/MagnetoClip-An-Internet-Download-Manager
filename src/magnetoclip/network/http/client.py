from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from ..auth.credentials import AuthSpec
from ..headers.builder import build_headers
from ..proxy.profiles import ProxySpec


@dataclass
class ClientConfig:
    """Configuration used to build an ``httpx.AsyncClient``."""

    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    timeout: float = 30.0
    max_connections: int = 64
    max_keepalive: int = 24
    proxy: ProxySpec | None = None
    auth: AuthSpec | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    verify_tls: bool = True


def build_client(
    config: ClientConfig | None = None,
    **overrides: Any,
) -> httpx.AsyncClient:
    """Create a configured ``httpx.AsyncClient`` for download operations."""
    config = config or ClientConfig()
    if overrides:
        config = replace(config, **overrides)

    kwargs: dict[str, Any] = {
        "headers": build_headers(config.user_agent, config.headers),
        "timeout": httpx.Timeout(config.timeout),
        "limits": httpx.Limits(
            max_connections=config.max_connections,
            max_keepalive_connections=config.max_keepalive,
        ),
        "follow_redirects": True,
        "verify": config.verify_tls,
    }

    if config.cookies:
        kwargs["cookies"] = dict(config.cookies)

    if config.proxy and config.proxy.type in ("http", "https"):
        proxy_url = config.proxy.to_url()
        if proxy_url:
            kwargs["proxy"] = proxy_url

    if config.auth:
        auth = config.auth.to_httpx_auth()
        if auth is not None:
            kwargs["auth"] = auth
        extra_headers = config.auth.headers()
        if extra_headers:
            kwargs["headers"].update(extra_headers)

    return httpx.AsyncClient(**kwargs)
