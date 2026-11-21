"""Per-torrent download orchestrator — bridges libtorrent alerts to the event bus."""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path
from typing import Any

from magnetoclip.core.events.bus import EventBus, Events
from magnetoclip.services.logging.setup import get_logger

from .client import TorrentClient, _parse_magnet, available
from .resume import TorrentResumeState, sidecar_path_for
from .types import TorrentSpec, TorrentStatus

log = get_logger(__name__)

# Event names (duplicated here to avoid a heavy import cycle; keep in sync
# with ``magnetoclip.core.events.bus.Events``).
_NAME_RESOLVED = "torrent.name_resolved"

# libtorrent alert type names we care about
_ALERT_ADD_TORRENT = "add_torrent_alert"
_ALERT_TORRENT_FINISHED = "torrent_finished_alert"
_ALERT_TORRENT_ERROR = "torrent_error_alert"
_ALERT_TORRENT_PAUSED = "torrent_paused_alert"
_ALERT_TRACKER_ERROR = "tracker_error_alert"
_ALERT_METADATA_RECEIVED = "metadata_received_alert"
_ALERT_PIECE_FINISHED = "piece_finished_alert"
_ALERT_STATE_CHANGED = "state_changed_alert"
_ALERT_SAVE_RESUME = "save_resume_data_alert"

# libtorrent 2.x state enum values
_LT_STATE_MAP = {
    0: "queued_for_checking",
    1: "checking_files",
    2: "downloading_metadata",
    3: "downloading",
    4: "finished",
    5: "seeding",
    6: "allocating",
    7: "checking_resume_data",
}


class TorrentDownloadHandler:
    """Runs the torrent download lifecycle for a single download.

    Mirrors progress/state events into the event bus so the DB and UI stay
    in sync — same pattern as the HTTP engine and yt-dlp streaming handler.
    """

    def __init__(
        self,
        client: TorrentClient,
        bus: EventBus,
        spec: TorrentSpec,
    ) -> None:
        self.client = client
        self.bus = bus
        self.spec = spec
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()
        self._done_event = threading.Event()
        self._error: str | None = None
        self._state = "queued"
        self._last_resume_save = 0.0
        self._metadata_ready = False
        self._resume_interval = 45.0

    async def run(self) -> str:
        """Execute the torrent download. Returns terminal state string."""
        if not available():
            return "failed"

        self.bus.post(
            Events.DOWNLOAD_STATE_CHANGED,
            {"id": self.spec.download_id, "state": "connecting"},
        )

        try:
            handle = await self._add_torrent()
            if handle is None:
                self._error = "Failed to add torrent to session"
                return "failed"

            if self.spec.sequential:
                self.client.set_sequential(self.spec.download_id, True)
            if self.spec.file_priorities:
                self.client.set_file_priorities(
                    self.spec.download_id, self.spec.file_priorities
                )
            if self.spec.upload_limit > 0:
                self.client.set_upload_limit(
                    self.spec.download_id, self.spec.upload_limit
                )
            if self.spec.download_limit > 0:
                self.client.set_download_limit(
                    self.spec.download_id, self.spec.download_limit
                )
            if self.spec.connections_max > 0:
                self.client.set_max_connections(
                    self.spec.download_id, self.spec.connections_max
                )

            self.bus.post(
                Events.DOWNLOAD_STATE_CHANGED,
                {"id": self.spec.download_id, "state": "downloading"},
            )

            result = await self._poll_until_done()
            return result
        except Exception as exc:
            log.warning(
                "torrent_download_failed",
                download_id=self.spec.download_id,
                error=str(exc),
            )
            self._error = str(exc)
            return "failed"
        finally:
            self._done_event.set()

    async def _add_torrent(self) -> Any | None:
        """Add the torrent to the session and return the handle."""
        loop = asyncio.get_running_loop()
        save_dir = self.spec.save_dir
        save_dir.mkdir(parents=True, exist_ok=True)

        # Try fast-resume first: a sidecar from a previous run lets libtorrent
        # skip the full re-check and continue from the saved piece state.
        resume_bytes = self._load_resume_bytes()
        handle = None
        if resume_bytes is not None:
            try:
                handle = await loop.run_in_executor(
                    None,
                    self.client.add_with_resume,
                    self.spec.download_id,
                    resume_bytes,
                    save_dir,
                )
                log.info(
                    "torrent_added_from_resume",
                    download_id=self.spec.download_id,
                    info_hash=self.spec.magnet_uri or self.spec.torrent_file_path,
                )
            except Exception as exc:
                log.warning(
                    "torrent_resume_add_failed",
                    download_id=self.spec.download_id,
                    error=str(exc),
                )
                handle = None

        if handle is None:
            if self.spec.magnet_uri:
                handle = await loop.run_in_executor(
                    None,
                    self.client.add_magnet,
                    self.spec.download_id,
                    self.spec.magnet_uri,
                    save_dir,
                )
            elif self.spec.torrent_file_path:
                handle = await loop.run_in_executor(
                    None,
                    self.client.add_torrent_file,
                    self.spec.download_id,
                    self.spec.torrent_file_path,
                    save_dir,
                )
            else:
                self._error = "No magnet URI or .torrent file provided"
                log.warning(
                    "torrent_add_failed_no_source",
                    download_id=self.spec.download_id,
                )
                return None

        self.client.register_resume_path(
            self.spec.download_id,
            self.spec.resume_sidecar
            or str(sidecar_path_for(str(self.spec.final_path))),
        )

        log.info(
            "torrent_added_to_session",
            download_id=self.spec.download_id,
            has_magnet=bool(self.spec.magnet_uri),
            has_file=bool(self.spec.torrent_file_path),
        )

        # Wait for metadata (for magnet URIs) with a timeout
        if self.spec.magnet_uri:
            await self._wait_for_metadata(handle)
            try:
                self._metadata_ready = bool(handle.has_metadata())
            except Exception:
                self._metadata_ready = False
        else:
            self._metadata_ready = True

        self._publish_name_if_available(handle)

        return handle

    def _load_resume_bytes(self) -> bytes | None:
        """Load and validate fast-resume data from the sidecar, if present."""
        try:
            path = Path(
                self.spec.resume_sidecar
                or str(sidecar_path_for(str(self.spec.final_path)))
            )
            if not path.exists():
                return None
            state = TorrentResumeState.load(path)
            if state is None or not state.resume_data_b64:
                return None
            buf = base64.b64decode(state.resume_data_b64)
        except Exception:
            return None

        expected = _parse_magnet(self.spec.magnet_uri) if self.spec.magnet_uri else ""
        if expected:
            actual = self.client.resume_info_hash(buf)
            if actual and actual.lower() != expected.lower():
                log.warning(
                    "torrent_resume_hash_mismatch",
                    download_id=self.spec.download_id,
                    expected=expected,
                    actual=actual,
                )
                return None
        return buf

    def _publish_name_if_available(self, handle: Any) -> None:
        """Publish the real torrent title to the bus once metadata arrives.

        Magnets without a ``dn=`` display name only get a real title after
        libtorrent fetches the metadata from peers. When that name becomes
        known, we forward it so the manager can store it and the UI Name
        column can stop showing the 'magnet_' placeholder.
        """
        try:
            if not handle.has_metadata():
                return
        except Exception:
            return
        try:
            raw = self.client.get_status(self.spec.download_id)
        except Exception:
            return
        if not raw:
            return
        name = (raw.get("name") or "").strip()
        if not name:
            return
        # If the spec already carried a real title (e.g. a .torrent file or a
        # magnet with dn=) don't clobber the more specific user-provided name
        # with the single-file torrent directory name.
        if name == self.spec.filename:
            return
        self.bus.post(
            _NAME_RESOLVED,
            {
                "id": self.spec.download_id,
                "filename": name,
                "info_hash": raw.get("info_hash") or "",
            },
        )

    async def _wait_for_metadata(self, handle: Any, timeout: float = 120.0) -> None:
        """Wait for torrent metadata to arrive (magnet links need this)."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self._cancel_event.is_set():
                return
            try:
                if handle.has_metadata():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        log.warning("torrent_metadata_timeout", download_id=self.spec.download_id)

    async def _poll_until_done(self) -> str:
        """Poll the torrent status until completion, failure, or cancellation."""
        last_status_post = 0.0
        null_count = 0
        while True:
            if self._cancel_event.is_set():
                self._maybe_request_resume(force=True)
                self.client.cancel_torrent(self.spec.download_id)
                return "stopped"

            # Process alerts (also while paused, so resume data still lands).
            await self._process_alerts()

            if self._pause_event.is_set():
                self._maybe_request_resume()
                await asyncio.sleep(0.5)
                continue

            # Post status update
            now = asyncio.get_event_loop().time()
            if now - last_status_post >= 1.0:
                last_status_post = now
                status = await self._get_status()
                if status is None:
                    null_count += 1
                    if null_count >= 5:
                        self._error = "Torrent handle lost — download may have been removed"
                        return "failed"
                    await asyncio.sleep(0.25)
                    continue
                null_count = 0
                self._post_status(status)
                self._maybe_request_resume()
                if status.state in ("finished", "seeding"):
                    if self.spec.seed_mode:
                        self._state = "seeding"
                        return "completed"
                    else:
                        self.client.pause_torrent(self.spec.download_id)
                        return "completed"
                if status.error:
                    self._error = status.error
                    return "failed"

            await asyncio.sleep(0.25)

    def _maybe_request_resume(self, force: bool = False) -> None:
        """Periodically ask libtorrent to flush fast-resume data."""
        if not self._metadata_ready:
            return
        now = time.monotonic()
        if not force and now - self._last_resume_save < self._resume_interval:
            return
        self._last_resume_save = now
        self.client.request_save_resume(self.spec.download_id)

    async def _get_status(self) -> TorrentStatus | None:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None, self.client.get_status, self.spec.download_id
        )
        if raw is None:
            return None
        state_str = _LT_STATE_MAP.get(raw["state"], "unknown")
        return TorrentStatus(
            download_id=self.spec.download_id,
            state=state_str,
            progress=raw["progress"],
            downloaded=raw["downloaded"],
            total=raw["total"],
            download_speed=raw["download_speed"],
            upload_speed=raw["upload_speed"],
            num_peers=raw["num_peers"],
            num_seeds=raw["num_seeds"],
            num_pieces=raw["num_pieces"],
            piece_size=raw["piece_size"],
            ratio=raw["ratio"],
            all_time_download=raw["all_time_download"],
            all_time_upload=raw["all_time_upload"],
            info_hash=raw["info_hash"],
            name=raw["name"],
            error=raw.get("error"),
        )

    async def _process_alerts(self) -> None:
        loop = asyncio.get_running_loop()
        alerts = await loop.run_in_executor(None, self.client.pop_alerts)
        for alert in alerts:
            alert_type = type(alert).__name__
            if alert_type == _ALERT_TORRENT_FINISHED:
                log.info(
                    "torrent_finished",
                    download_id=self.spec.download_id,
                )
            elif alert_type == _ALERT_TORRENT_ERROR:
                error_msg = str(getattr(alert, "error", "unknown error"))
                log.warning(
                    "torrent_alert_error",
                    download_id=self.spec.download_id,
                    error=error_msg,
                )
                self._error = error_msg
            elif alert_type == _ALERT_METADATA_RECEIVED:
                log.info(
                    "torrent_metadata_received",
                    download_id=self.spec.download_id,
                )
                self._metadata_ready = True
            elif alert_type == _ALERT_SAVE_RESUME:
                self.client.capture_resume_alert(alert)

    def _post_status(self, status: TorrentStatus) -> None:
        """Post progress and state events to the bus."""
        self.bus.post(
            Events.PROGRESS_UPDATED,
            {
                "id": status.download_id,
                "downloaded": status.downloaded,
                "total": status.total,
                "num_peers": status.num_peers,
                "num_seeds": status.num_seeds,
                "num_pieces": status.num_pieces,
                "piece_size": status.piece_size,
            },
        )
        self.bus.post(
            Events.SPEED_UPDATED,
            {
                "id": status.download_id,
                "speed": float(status.download_speed),
            },
        )
        self.bus.post(
            Events.CONNECTIONS_UPDATED,
            {
                "id": status.download_id,
                "active": status.num_peers,
                # Per-torrent cap (0 means the session-wide limit applies, so
                # the manager keeps whatever value it already stored).
                "max": self.spec.connections_max,
            },
        )

        # Once the real name is known, forward it so the manager can store it
        # and refresh the Name column (which may currently show 'magnet_').
        name = (status.name or "").strip()
        if name and name != self.spec.filename:
            self.bus.post(
                _NAME_RESOLVED,
                {
                    "id": status.download_id,
                    "filename": name,
                    "info_hash": status.info_hash,
                },
            )

    def cancel(self) -> None:
        self._cancel_event.set()

    def pause(self) -> None:
        """Pause the torrent — stops polling and pauses the libtorrent handle.

        Flushes fast-resume data right away so a relaunch while paused can
        continue instead of re-checking a stale sidecar.
        """
        self._pause_event.set()
        try:
            self.client.pause_torrent(self.spec.download_id)
        except Exception as exc:
            log.warning("torrent_pause_failed", download_id=self.spec.download_id, error=str(exc))
        self._metadata_ready = True
        self._last_resume_save = 0.0
        self._maybe_request_resume(force=True)

    def resume(self) -> None:
        """Resume a paused torrent — resumes the libtorrent handle and polling."""
        try:
            self.client.resume_torrent(self.spec.download_id)
        except Exception as exc:
            log.warning("torrent_resume_failed", download_id=self.spec.download_id, error=str(exc))
        self._pause_event.clear()

    def is_paused(self) -> bool:
        """True while the handler is parked by pause()."""
        return self._pause_event.is_set()

    def is_done(self) -> bool:
        return self._done_event.is_set()

    @property
    def error(self) -> str | None:
        return self._error
