"""libtorrent session wrapper — manages the BitTorrent engine singleton."""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)

_HAS_LIBTORRENT = False
_lt: Any = None

try:
    import libtorrent as _lt

    _HAS_LIBTORRENT = True
except ImportError:
    log.info("libtorrent_not_available")


def available() -> bool:
    return _HAS_LIBTORRENT


def _lt_session() -> Any:
    return _lt


def _parse_magnet(magnet_uri: str) -> str:
    """Extract info_hash from a magnet URI."""
    match = re.search(r"btih:([A-Fa-f0-9]{40}|[A-Za-z0-9]{32})", magnet_uri)
    if match:
        return match.group(1).lower()
    return ""


def _info_hash_to_hex(info_hash_bytes: bytes) -> str:
    if len(info_hash_bytes) == 20:
        return info_hash_bytes.hex()
    return hashlib.sha1(info_hash_bytes).hexdigest()


@dataclass
class ClientConfig:
    listen_port: int = 6881
    enable_dht: bool = True
    enable_pex: bool = True
    enable_encryption: bool = True
    max_connections: int = 200
    max_uploads: int = 4
    user_agent: str = "MagnetoClip/0.1"
    save_path: str = ""


class TorrentClient:
    """Singleton wrapper around a libtorrent session.

    Alert processing runs via a polling loop that can be driven by Qt's timer
    or by asyncio.sleep(). All blocking libtorrent getters are dispatched to a
    thread to avoid freezing the GUI.
    """

    def __init__(self, config: ClientConfig | None = None) -> None:
        if not _HAS_LIBTORRENT:
            raise RuntimeError("libtorrent is not installed")
        self._config = config or ClientConfig()
        self._session = self._create_session()
        self._handles: dict[int, Any] = {}  # download_id -> torrent_handle
        self._handle_map: dict[str, int] = {}  # info_hash_hex -> download_id
        self._alerts: list[Any] = []
        self._lock = threading.Lock()
        self._running = True
        self._alert_thread: threading.Thread | None = None
        log.info("torrent_client_created")

    def _create_session(self) -> Any:
        lt = _lt_session()
        settings = {
            "listen_interfaces": f"0.0.0.0:{self._config.listen_port}",
            "enable_dht": self._config.enable_dht,
            "enable_lsd": True,
            "enable_natpmp": True,
            "enable_upnp": True,
            "anonymous_mode": False,
            "user_agent": self._config.user_agent,
            "connections_limit": self._config.max_connections,
            "unchoke_slots_limit": self._config.max_uploads,
        }
        if self._config.enable_pex:
            settings["enable_incoming_utp"] = True
            settings["enable_outgoing_utp"] = True
            settings["enable_incoming_tcp"] = True
            settings["enable_outgoing_tcp"] = True
        if self._config.enable_encryption:
            settings["out_enc_policy"] = 1  # enabled
            settings["in_enc_policy"] = 1
            settings["allowed_enc_level"] = 3  # plaintext + RC4
        ses = lt.session(settings)
        if self._config.enable_dht:
            for host, port in [
                ("router.bittorrent.com", 6881),
                ("dht.transmissionbt.com", 6881),
                ("router.utorrent.com", 6881),
                ("dht.aelitis.com", 6881),
            ]:
                ses.add_dht_node((host, port))
        return ses

    @property
    def session(self) -> Any:
        return self._session

    def add_magnet(self, download_id: int, magnet_uri: str, save_dir: Path) -> Any:
        """Add a torrent from a magnet URI. Returns the torrent handle."""
        lt = _lt_session()
        params = {
            "save_path": str(save_dir),
        }
        handle = lt.add_magnet_uri(self._session, magnet_uri, params)
        with self._lock:
            self._handles[download_id] = handle
            info_hash = str(handle.info_hash())
            self._handle_map[info_hash] = download_id
        log.info("magnet_added", download_id=download_id, info_hash=info_hash)
        return handle

    def add_torrent_file(
        self, download_id: int, torrent_path: str, save_dir: Path
    ) -> Any:
        """Add a torrent from a .torrent file. Returns the torrent handle."""
        lt = _lt_session()
        info = lt.torrent_info(str(torrent_path))
        params = {
            "ti": info,
            "save_path": str(save_dir),
        }
        handle = self._session.add_torrent(params)
        with self._lock:
            self._handles[download_id] = handle
            info_hash = str(handle.info_hash())
            self._handle_map[info_hash] = download_id
        log.info("torrent_file_added", download_id=download_id, info_hash=info_hash)
        return handle

    def pause_torrent(self, download_id: int) -> bool:
        handle = self._handles.get(download_id)
        if handle is None:
            return False
        try:
            handle.pause()
            return True
        except Exception as exc:
            log.warning("torrent_pause_failed", download_id=download_id, error=str(exc))
            return False

    def resume_torrent(self, download_id: int) -> bool:
        handle = self._handles.get(download_id)
        if handle is None:
            return False
        try:
            handle.resume()
            return True
        except Exception as exc:
            log.warning(
                "torrent_resume_failed", download_id=download_id, error=str(exc)
            )
            return False

    def cancel_torrent(self, download_id: int) -> bool:
        handle = self._handles.get(download_id)
        if handle is None:
            return False
        try:
            self._session.remove_torrent(handle)
        except Exception as exc:
            log.warning(
                "torrent_cancel_failed", download_id=download_id, error=str(exc)
            )
        with self._lock:
            self._handles.pop(download_id, None)
            if handle is not None:
                info_hash = str(handle.info_hash())
                self._handle_map.pop(info_hash, None)
        return True

    def get_status(self, download_id: int) -> dict[str, Any] | None:
        """Get torrent status. Called from threads to avoid blocking the GUI."""
        handle = self._handles.get(download_id)
        if handle is None:
            return None
        try:
            status = handle.status()
            info = handle.torrent_file()
            name = info.name() if info else ""
            total = info.total_size() if info else status.total
            num_pieces = info.num_pieces() if info else 0
            piece_size = info.piece_length() if info else 0
            return {
                "download_id": download_id,
                "progress": status.progress,
                "downloaded": status.total_done,
                "total": total,
                "download_speed": status.download_rate,
                "upload_speed": status.upload_rate,
                "num_peers": status.num_peers,
                "num_seeds": status.num_seeds,
                "num_pieces": num_pieces,
                "piece_size": piece_size,
                "ratio": status.all_time_upload / max(status.all_time_download, 1),
                "all_time_download": status.all_time_download,
                "all_time_upload": status.all_time_upload,
                "info_hash": str(handle.info_hash()),
                "name": name,
                "state": int(status.state),
                "error": str(status.error) if status.error else None,
            }
        except Exception as exc:
            log.warning("torrent_status_failed", download_id=download_id, error=str(exc))
            return None

    def set_upload_limit(self, download_id: int, limit: int) -> None:
        handle = self._handles.get(download_id)
        if handle is not None:
            handle.set_upload_limit(limit if limit > 0 else -1)

    def set_download_limit(self, download_id: int, limit: int) -> None:
        handle = self._handles.get(download_id)
        if handle is not None:
            handle.set_download_limit(limit if limit > 0 else -1)

    def set_sequential(self, download_id: int, sequential: bool) -> None:
        handle = self._handles.get(download_id)
        if handle is None:
            return
        if sequential:
            flags = handle.flags()
            flags |= 0x04  # sequential_download flag
            handle.set_flags(flags)
        else:
            flags = handle.flags()
            flags &= ~0x04
            handle.unset_flags(flags)

    def set_file_priorities(
        self, download_id: int, priorities: list[int]
    ) -> None:
        handle = self._handles.get(download_id)
        if handle is not None:
            handle.prioritize_files(priorities)

    def save_resume(self, download_id: int) -> bytes | None:
        handle = self._handles.get(download_id)
        if handle is None:
            return None
        try:
            lt = _lt_session()
            return lt.write_resume_data_buf(handle)
        except Exception as exc:
            log.warning(
                "torrent_resume_save_failed",
                download_id=download_id,
                error=str(exc),
            )
            return None

    def pop_alerts(self) -> list[Any]:
        """Pop all pending alerts from the session."""
        if not _HAS_LIBTORRENT:
            return []
        try:
            alerts = self._session.pop_alerts()
            return alerts
        except Exception:
            return []

    def resolve_download_id(self, info_hash: str) -> int | None:
        with self._lock:
            return self._handle_map.get(info_hash)

    def has_handle(self, download_id: int) -> bool:
        return download_id in self._handles

    async def shutdown(self) -> None:
        self._running = False
        for download_id in list(self._handles.keys()):
            self.cancel_torrent(download_id)
        self._session = None
        log.info("torrent_client_shutdown")
