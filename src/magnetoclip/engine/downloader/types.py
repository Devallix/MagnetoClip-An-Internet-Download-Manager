from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from magnetoclip.network.auth.credentials import AuthSpec
from magnetoclip.network.proxy.profiles import ProxySpec


@dataclass
class DownloadSpec:
    """Everything the engine needs to download one file."""

    download_id: int
    url: str
    save_dir: Path
    filename: str
    headers: dict[str, str] = field(default_factory=dict)
    auth: AuthSpec | None = None
    proxy: ProxySpec | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    connections_max: int = 8
    retry_max: int = 5
    retry_base: float = 1.0
    timeout: float = 30.0
    hash_algo: str | None = None
    hash_expected: str | None = None
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    priority: int = 0

    @property
    def final_path(self) -> Path:
        return self.save_dir / self.filename

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "url": self.url,
            "save_dir": str(self.save_dir),
            "filename": self.filename,
            "headers": self.headers,
            "auth": self.auth.to_dict() if self.auth else None,
            "proxy": self.proxy.to_dict() if self.proxy else None,
            "cookies": self.cookies,
            "connections_max": self.connections_max,
            "retry_max": self.retry_max,
            "retry_base": self.retry_base,
            "timeout": self.timeout,
            "hash_algo": self.hash_algo,
            "hash_expected": self.hash_expected,
            "user_agent": self.user_agent,
            "priority": self.priority,
        }
