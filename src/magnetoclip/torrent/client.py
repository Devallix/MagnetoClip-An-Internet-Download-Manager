"""libtorrent session wrapper — manages the BitTorrent engine singleton."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from magnetoclip.services.logging.setup import get_logger

from .resume import TorrentResumeState

log = get_logger(__name__)

_HAS_LIBTORRENT = False
_lt: Any = None

# libtorrent expresses ``cache_size`` as a count of 16 KiB blocks.
_CACHE_BLOCK_BYTES = 16 * 1024

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
    max_connections: int = 500
    max_uploads: int = 4
    max_download_speed: int = 0  # bytes/s; 0 = unlimited
    max_upload_speed: int = 0  # bytes/s; 0 = unlimited
    user_agent: str = "MagnetoClip/0.1"
    save_path: str = ""

    # ---- throughput tuning ----
    # libtorrent's default disk cache is ~32 MiB, which starves fast links and
    # leaves pieces waiting on disk rather than the network. Cache in MiB; 0
    # keeps libtorrent's own default.
    disk_cache_mb: int = 256
    # Threads hashing pieces while other pieces keep downloading.
    hashing_threads: int = 4
    # Threads servicing disk reads/writes.
    aio_threads: int = 16
    # Outstanding block requests per peer. The 500 default underfills
    # high-latency, high-bandwidth ("long fat") links.
    max_out_request_queue: int = 1500
    # Drop unresponsive peers sooner so their slots go to healthier ones.
    peer_connect_timeout: int = 10
    # Open-file handle cache; matters for torrents with many files.
    file_pool_size: int = 100
    max_peerlist_size: int = 8000
    # Favour TCP peers over uTP for raw throughput (uTP stays available).
    prefer_tcp: bool = True

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> ClientConfig:
        """Build a config from the app settings store.

        The ``torrent.max_*_speed`` settings are stored in KB/s; libtorrent
        expects bytes/s, so they are converted here (0 stays unlimited).
        """
        return cls(
            listen_port=int(settings.get("torrent.listen_port", 6881)),
            enable_dht=bool(settings.get("torrent.enable_dht", True)),
            enable_pex=bool(settings.get("torrent.enable_pex", True)),
            enable_encryption=bool(settings.get("torrent.enable_encryption", True)),
            max_connections=int(settings.get("torrent.max_connections", 500)),
            max_uploads=int(settings.get("torrent.max_uploads", 4)),
            max_download_speed=int(settings.get("torrent.max_download_speed", 0)) * 1024,
            max_upload_speed=int(settings.get("torrent.max_upload_speed", 0)) * 1024,
            disk_cache_mb=int(settings.get("torrent.disk_cache_mb", 256)),
            hashing_threads=int(settings.get("torrent.hashing_threads", 4)),
            max_out_request_queue=int(
                settings.get("torrent.max_out_request_queue", 1500)
            ),
            prefer_tcp=bool(settings.get("torrent.prefer_tcp", True)),
        )


def _cache_blocks(mb: int) -> int:
    """Convert a disk-cache size in MiB to libtorrent's 16 KiB block count."""
    return max(int(mb), 0) * 1024 * 1024 // _CACHE_BLOCK_BYTES


def build_tuning_settings(config: ClientConfig) -> dict[str, Any]:
    """Session settings that trade memory for torrent throughput.

    Shared by session construction (:func:`build_session_settings`) and live
    application (:meth:`TorrentClient.apply_tuning`) so a settings change can
    take effect without restarting the session.
    """
    tuning: dict[str, Any] = {
        "aio_threads": config.aio_threads,
        "hashing_threads": config.hashing_threads,
        "max_out_request_queue": config.max_out_request_queue,
        "peer_connect_timeout": config.peer_connect_timeout,
        "file_pool_size": config.file_pool_size,
        "max_peerlist_size": config.max_peerlist_size,
        # 0 = prefer_tcp, 1 = peer_proportional.
        "mixed_mode_algorithm": 0 if config.prefer_tcp else 1,
    }
    if config.disk_cache_mb > 0:
        cache_bytes = config.disk_cache_mb * 1024 * 1024
        tuning["cache_size"] = _cache_blocks(config.disk_cache_mb)
        # Let a cache's worth of data queue before throttling disk I/O.
        tuning["max_queued_disk_bytes"] = max(100 * 1024 * 1024, cache_bytes)
    return tuning


def build_session_settings(
    config: ClientConfig, lt: Any = None
) -> dict[str, Any]:
    """Assemble the libtorrent session settings dict for *config*.

    Pure and side-effect free so it can be unit-tested without starting a
    session. When *lt* is provided, every key is checked against the running
    libtorrent build and anything it does not recognise is dropped (logged
    rather than fatal, so a renamed setting cannot block startup).
    """
    lt = _lt if lt is None else lt
    settings: dict[str, Any] = {
        # Listen on both IPv4 and IPv6 so peers on either stack can reach us.
        "listen_interfaces": (
            f"0.0.0.0:{config.listen_port},[::]:{config.listen_port}"
        ),
        "enable_dht": config.enable_dht,
        "enable_lsd": True,
        "enable_natpmp": True,
        "enable_upnp": True,
        "anonymous_mode": False,
        "user_agent": config.user_agent,
        "connections_limit": config.max_connections,
        "unchoke_slots_limit": config.max_uploads,
        # Raise the naive connection attempt rate and the initial connect burst
        # so peer lists fill quickly; libtorrent defaults ramp up slowly.
        "connection_speed": 50,
        "torrent_connect_boost": 50,
        # uTP and TCP are transports — always on, independent of PEX.
        "enable_incoming_utp": True,
        "enable_outgoing_utp": True,
        "enable_incoming_tcp": True,
        "enable_outgoing_tcp": True,
    }
    settings.update(build_tuning_settings(config))
    if config.max_download_speed > 0:
        settings["download_rate_limit"] = config.max_download_speed
    if config.max_upload_speed > 0:
        settings["upload_rate_limit"] = config.max_upload_speed
    if config.enable_encryption:
        settings["out_enc_policy"] = 1  # enabled
        settings["in_enc_policy"] = 1
        settings["allowed_enc_level"] = 3  # plaintext + RC4
    if lt is not None:
        # Error/status/tracker/storage alerts. Storage is required for
        # save_resume_data_alert (fast-resume) to be posted at all.
        settings["alert_mask"] = int(
            lt.alert.category_t.error_notification
            | lt.alert.category_t.status_notification
            | lt.alert.category_t.tracker_notification
            | lt.alert.category_t.storage_notification
        )
        known = lt.default_settings()
        unknown = [key for key in settings if key not in known]
        for key in unknown:
            settings.pop(key, None)
        if unknown:
            log.warning("torrent_session_settings_unknown", keys=unknown)
    return settings


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
        self._resume_paths: dict[int, str] = {}  # download_id -> .mctorrent path
        self._resume_buffers: dict[str, bytes] = {}  # info_hash_hex -> resume data
        self._alerts: list[Any] = []
        self._lock = threading.Lock()
        self._running = True
        self._alert_thread: threading.Thread | None = None
        log.info("torrent_client_created")

    def _create_session(self) -> Any:
        lt = _lt_session()
        ses = lt.session(build_session_settings(self._config, lt))
        if self._config.enable_dht:
            for host, port in [
                ("router.bittorrent.com", 6881),
                ("dht.transmissionbt.com", 6881),
                ("router.utorrent.com", 6881),
                ("dht.aelitis.com", 6881),
                ("dht.libtorrent.org", 25401),
            ]:
                ses.add_dht_node((host, port))
        return ses

    def _add_params_flags(self) -> Any:
        """Flags applied when adding a torrent (honours the PEX toggle)."""
        lt = _lt_session()
        flags = lt.torrent_flags.default_flags
        if not self._config.enable_pex:
            flags |= lt.torrent_flags.disable_pex
        return flags

    @property
    def session(self) -> Any:
        return self._session

    def add_magnet(self, download_id: int, magnet_uri: str, save_dir: Path) -> Any:
        """Add a torrent from a magnet URI. Returns the torrent handle."""
        lt = _lt_session()
        params = {
            "save_path": str(save_dir),
            "flags": self._add_params_flags(),
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
            "flags": self._add_params_flags(),
        }
        handle = self._session.add_torrent(params)
        with self._lock:
            self._handles[download_id] = handle
            info_hash = str(handle.info_hash())
            self._handle_map[info_hash] = download_id
        log.info("torrent_file_added", download_id=download_id, info_hash=info_hash)
        return handle

    def resume_info_hash(self, resume_bytes: bytes) -> str:
        """Return the info hash embedded in serialized resume data (or "")."""
        try:
            params = _lt_session().read_resume_data(resume_bytes)
            return str(params.info_hash)
        except Exception:
            return ""

    def add_with_resume(
        self, download_id: int, resume_bytes: bytes, save_dir: Path
    ) -> Any:
        """Add a torrent using previously saved resume data.

        This lets libtorrent skip the full file re-check on restart and resume
        from the exact piece state stored in the sidecar.
        """
        lt = _lt_session()
        params = lt.read_resume_data(resume_bytes)
        params.save_path = str(save_dir)
        if not self._config.enable_pex:
            params.flags = params.flags | lt.torrent_flags.disable_pex
        handle = self._session.add_torrent(params)
        with self._lock:
            self._handles[download_id] = handle
            info_hash = str(handle.info_hash())
            self._handle_map[info_hash] = download_id
        log.info(
            "torrent_resumed_from_data",
            download_id=download_id,
            info_hash=info_hash,
        )
        return handle

    def pause_torrent(self, download_id: int) -> bool:
        handle = self._handles.get(download_id)
        if handle is None:
            return False
        try:
            # Graceful pause: let the in-flight piece finish writing to disk so
            # a resume later continues exactly where the download stopped.
            flags = getattr(_lt.torrent_handle, "graceful_pause", 0) if _lt else 0
            handle.pause(flags)
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
            self._resume_paths.pop(download_id, None)
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
        lt = _lt_session()
        try:
            if sequential:
                handle.set_flags(lt.torrent_flags.sequential_download)
            else:
                handle.unset_flags(lt.torrent_flags.sequential_download)
        except Exception as exc:
            log.warning(
                "torrent_set_sequential_failed",
                download_id=download_id,
                error=str(exc),
            )

    def set_file_priorities(
        self, download_id: int, priorities: list[int]
    ) -> None:
        handle = self._handles.get(download_id)
        if handle is not None:
            handle.prioritize_files(priorities)

    def set_max_connections(self, download_id: int, limit: int) -> None:
        handle = self._handles.get(download_id)
        if handle is not None and limit > 0:
            handle.set_max_connections(limit)

    def apply_session_limits(
        self,
        *,
        max_connections: int | None = None,
        max_uploads: int | None = None,
        download_bps: int | None = None,
        upload_bps: int | None = None,
    ) -> None:
        """Apply connection/rate limits to the live session (no restart needed)."""
        if self._session is None:
            return
        settings: dict[str, int] = {}
        if max_connections is not None and max_connections > 0:
            settings["connections_limit"] = int(max_connections)
            self._config.max_connections = int(max_connections)
        if max_uploads is not None and max_uploads > 0:
            settings["unchoke_slots_limit"] = int(max_uploads)
            self._config.max_uploads = int(max_uploads)
        if download_bps is not None:
            settings["download_rate_limit"] = int(download_bps)
            self._config.max_download_speed = int(download_bps)
        if upload_bps is not None:
            settings["upload_rate_limit"] = int(upload_bps)
            self._config.max_upload_speed = int(upload_bps)
        if not settings:
            return
        try:
            self._session.apply_settings(settings)
            log.info("torrent_session_limits_applied", **settings)
        except Exception as exc:
            log.warning("torrent_session_limits_failed", error=str(exc))

    def apply_tuning(self, config: ClientConfig) -> None:
        """Apply throughput-tuning settings to the live session.

        libtorrent accepts all of these at runtime, so a change in the settings
        page takes effect immediately instead of on the next restart.
        """
        if self._session is None:
            return
        self._config = config
        settings = build_tuning_settings(config)
        try:
            self._session.apply_settings(settings)
            log.info("torrent_session_tuning_applied", **settings)
        except Exception as exc:
            log.warning("torrent_session_tuning_failed", error=str(exc))

    def register_resume_path(self, download_id: int, sidecar_path: str) -> None:
        with self._lock:
            self._resume_paths[download_id] = sidecar_path

    def request_save_resume(self, download_id: int) -> bool:
        """Ask libtorrent to emit resume data for this torrent (async alert)."""
        handle = self._handles.get(download_id)
        if handle is None:
            return False
        try:
            handle.save_resume_data()
            return True
        except Exception as exc:
            log.warning(
                "torrent_resume_request_failed",
                download_id=download_id,
                error=str(exc),
            )
            return False

    def capture_resume_alert(self, alert: Any) -> None:
        """Persist resume data when a ``save_resume_data_alert`` arrives.

        Called by the handlers that drain the session's alert queue; writing
        from here (rather than from a single owner) means the sidecar is saved
        even if a different handler happened to pop the alert.
        """
        try:
            info_hash = str(alert.handle.info_hash())
            buf = _lt_session().write_resume_data_buf(alert.params)
        except Exception:
            return
        with self._lock:
            download_id = self._handle_map.get(info_hash)
            sidecar = self._resume_paths.get(download_id) if download_id else None
        if download_id is None or not sidecar:
            return
        try:
            state = TorrentResumeState(
                download_id=download_id,
                info_hash=info_hash,
                resume_data_b64=base64.b64encode(buf).decode("ascii"),
            )
            state.save(Path(sidecar))
            log.info(
                "torrent_resume_saved",
                download_id=download_id,
                info_hash=info_hash,
                size=len(buf),
            )
        except Exception as exc:
            log.warning(
                "torrent_resume_save_failed",
                download_id=download_id,
                error=str(exc),
            )

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
        # Best-effort fast-resume flush: ask every torrent to emit resume data
        # and drain the resulting alerts before tearing the session down, so a
        # restart doesn't have to re-check in-progress files from scratch.
        if self._handles:
            for download_id in list(self._handles.keys()):
                self.request_save_resume(download_id)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                pending = self.pop_alerts()
                for alert in pending:
                    if type(alert).__name__ == "save_resume_data_alert":
                        self.capture_resume_alert(alert)
                if not pending:
                    await asyncio.sleep(0.1)
        for download_id in list(self._handles.keys()):
            self.cancel_torrent(download_id)
        self._session = None
        log.info("torrent_client_shutdown")
