from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # General
    "general.startup": True,
    "general.tray_behavior": "minimize_to_tray",
    "general.confirm_removal": True,
    # Downloads
    "downloads.default_directory": str(Path.home() / "Downloads"),
    "downloads.simultaneous": 3,
    "downloads.connections_per_download": 8,
    "downloads.auto_categorize": True,
    # Streaming (embedded/online media via yt-dlp)
    "streaming.quality": "best",
    # Torrent
    "torrent.default_save_dir": "",
    "torrent.listen_port": 6881,
    "torrent.enable_dht": True,
    "torrent.enable_pex": True,
    "torrent.enable_encryption": True,
    "torrent.max_upload_speed": 0,
    "torrent.max_download_speed": 0,
    "torrent.max_connections": 200,
    "torrent.max_uploads": 4,
    "torrent.default_sequential": False,
    "torrent.auto_seed": False,
    "torrent.search_sites": ["yts"],
    "torrent.file_association": False,
    "torrent.magnet_protocol": False,
    # Network
    "network.user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "network.timeout_seconds": 30,
    "network.retry_max": 5,
    "network.proxy_profile": "direct",
    "network.max_bandwidth_mbps": 0.0,
    "network.default_proxy_id": 0,
    # Browser
    "browser.integration_enabled": False,
    "browser.capture_enabled": True,
    "browser.default_downloader": False,
    "browser.confirm_capture": True,
    "browser.notify_downloadable": True,
    "browser.skip_all_until": "",
    # Scheduler
    "scheduler.enabled": False,
    # Appearance
    "appearance.theme": "dark",
    "appearance.accent": "#8B5CF6",
    "appearance.density": "comfortable",
    "appearance.animations": True,
    # Advanced
    "advanced.log_level": "info",
    "advanced.experimental": False,
    # Updates
    "updates.check_enabled": True,
    "updates.endpoint": "https://releases.magnetoclip.dev/manifest.json",
    "updates.last_checked": "",
    "updates.last_version": "",
}


class Settings:
    """Flat, key/value settings model with typed defaults.

    Keys use dotted names (e.g. ``downloads.simultaneous``). Persistence is
    delegated to a store that reads/writes the SQLite ``settings`` table.
    """

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values: dict[str, Any] = dict(DEFAULTS)
        if values:
            self._values.update(
                {k: v for k, v in values.items() if k in DEFAULTS}
            )

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if key in self._values:
            self._values[key] = value

    def merge(self, values: dict[str, Any] | None) -> None:
        """Apply stored overrides in place, ignoring unknown keys."""
        if values:
            self._values.update(
                {k: v for k, v in values.items() if k in DEFAULTS}
            )

    def keys(self) -> list[str]:
        return list(self._values)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._values)

    def to_store_dict(self) -> dict[str, Any]:
        return dict(self._values)

    @classmethod
    def from_store(cls, stored: dict[str, Any]) -> Settings:
        return cls(stored)
