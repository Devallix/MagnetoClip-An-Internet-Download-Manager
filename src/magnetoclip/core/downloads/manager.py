"""DownloadManager: application-level orchestration over MagnetoCore.

Responsibilities:
- accept new downloads (URL validation, filename sanitization, auto-categorize)
- start/pause/resume/cancel/restart/remove downloads with concurrency limits
- resume interrupted downloads from ``.mclip`` sidecars
- mirror engine progress/state events into the SQLite database
- apply global bandwidth from settings and the scheduler
- advance queue items when capacity frees up
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import sessionmaker

from ...app.context import AppContext
from ...database.models import Download, DownloadStatus
from ...database.repositories import DownloadRepository, QueueRepository
from ...engine.downloader.engine import MagnetoCore, spec_from_url
from ...engine.resume.mclip import MClipState
from ...intelligence import SpeedPredictor
from ...media.streaming import (
    DownloadCancelled,
    StreamResolutionError,
    download_stream,
    is_audio_platform,
    is_streaming_url,
    resolve_stream,
)
from ...network.http.client import ClientConfig
from ...security.safe_names import safe_join, sanitize_filename
from ...services.logging import get_logger
from ..events.bus import Events

log = get_logger(__name__)

ACTIVE_STATUSES = (
    DownloadStatus.connecting,
    DownloadStatus.downloading,
    DownloadStatus.retrying,
    DownloadStatus.verifying,
)
NON_TERMINAL = (
    DownloadStatus.queued,
    DownloadStatus.scheduled,
    DownloadStatus.connecting,
    DownloadStatus.downloading,
    DownloadStatus.retrying,
    DownloadStatus.verifying,
    DownloadStatus.paused,
)


class DownloadManager:
    """Coordinates download lifecycle, persistence, and the event bus."""

    def __init__(
        self,
        context: AppContext,
        *,
        core: MagnetoCore | None = None,
        categories=None,
        queues=None,
    ) -> None:
        self.context = context
        self.settings = context.settings
        self.session_factory: sessionmaker = context.session_factory
        self.events = context.events
        client_config = ClientConfig(
            user_agent=str(self.settings.get("network.user_agent", "MagnetoClip/0.1")),
            timeout=float(self.settings.get("network.timeout_seconds", 30)),
        )
        self.core = core or MagnetoCore(bus=context.events, client_config=client_config)
        self.categories = categories or context.categories
        self.queues = queues or context.queues

        self.semaphore = asyncio.Semaphore(
            int(self.settings.get("downloads.simultaneous", 3))
        )
        self._tasks: dict[int, asyncio.Task] = {}
        self._last_progress_write: dict[int, float] = {}
        self._predictor = SpeedPredictor()
        self._etas: dict[int, float | None] = {}
        self._stream_cancel: dict[int, threading.Event] = {}

        self.events.connect(Events.DOWNLOAD_STATE_CHANGED, self._on_state_changed)
        self.events.connect(Events.PROGRESS_UPDATED, self._on_progress)
        self.events.connect(Events.SPEED_UPDATED, self._on_speed)
        self.events.connect(Events.CONNECTIONS_UPDATED, self._on_connections)
        self.events.connect(Events.NETWORK_CHANGED, self._on_network_changed)
        self.events.connect(Events.SETTINGS_CHANGED, self._on_settings_changed)

        self._apply_bandwidth()

    # ----- creation -----

    def add(
        self,
        url: str,
        *,
        filename: str | None = None,
        save_dir: Path | str | None = None,
        category_name: str | None = None,
        queue_id: int | None = None,
        priority: int = 0,
        connections_max: int | None = None,
        headers: dict[str, str] | None = None,
        hash_algo: str | None = None,
        hash_expected: str | None = None,
        proxy_profile_id: int | None = None,
        auth_username: str | None = None,
        auth_password: str | None = None,
        cookies: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> Download:
        """Validate the URL and persist a new download record.

        ``data`` lets a capture hand over in-memory bytes (e.g. a Telegram
        ``blob:`` image the extension fetched for us). When set, no network
        request happens: the bytes are written straight to disk and the record
        is created as completed. The URL may then be a ``blob:`` URI.
        """
        if data is None:
            self._validate_url(url)
        name = sanitize_filename(filename) if filename else self._derive_name(url)
        category = None
        if category_name:
            category = self.categories.get_by_name(category_name)
        streaming = is_streaming_url(url)
        stream_kind = None
        if streaming and self.settings.get("downloads.auto_categorize", True):
            stream_kind = "audio" if is_audio_platform(url) else "video"
            category = self.categories.get_by_name(
                "Music" if stream_kind == "audio" else "Videos"
            )
        if category is None and self.settings.get("downloads.auto_categorize", True):
            category = self.categories.classify(name, url)
        target_dir = self._resolve_save_dir(save_dir, category)
        final_path = safe_join(target_dir, name)

        connections = connections_max or int(
            self.settings.get("downloads.connections_per_download", 8)
        )
        auth_ref = self._store_auth_ref(auth_username, auth_password)
        if proxy_profile_id is None:
            proxy_profile_id = int(
                self.settings.get("network.default_proxy_id", 0) or 0
            ) or None
        if data is not None:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(data)
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.add(
                url,
                filename=final_path.name,
                save_path=str(final_path),
                category_id=category.id if category else None,
                queue_id=queue_id,
                priority=priority,
                connections_max=connections,
                headers=self._merge_cookie_header(headers, cookies),
                hash_algo=hash_algo,
                hash_expected=hash_expected,
                proxy_profile_id=proxy_profile_id,
                auth_ref=auth_ref,
            )
            if data is not None:
                download.status = DownloadStatus.completed
                download.size_total = len(data)
                download.size_downloaded = len(data)
                download.completed_at = datetime.now(UTC)
            from ...media.detect import detect_type

            if stream_kind:
                download.detected_type = stream_kind
            else:
                download.detected_type = detect_type(filename=final_path.name, url=url)
            session.commit()
        self.events.post(Events.DOWNLOAD_ADDED, self.snapshot_item(download))
        return download

    # ----- lifecycle -----

    def start(self, download_id: int, *, queue_advance: bool = False) -> bool:
        """Kick off ``download_id``; returns False if it is already running."""
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        if download is None:
            return False
        if (
            download.status in ACTIVE_STATUSES
            or download_id in self._tasks
            or download.status == DownloadStatus.completed
        ):
            return False

        if is_streaming_url(download.url):
            return self._start_streaming(download_id)

        spec = self._build_spec(download)
        state = self._load_resume_state(spec)
        if state is not None:
            self._sync_state_to_db(download_id, state)
        else:
            with self.session_factory() as session:
                repo = DownloadRepository(session)
                download = repo.get(download_id)
                if download is not None and download.status not in (
                    DownloadStatus.paused,
                    DownloadStatus.queued,
                    DownloadStatus.scheduled,
                ):
                    download.status = DownloadStatus.scheduled
                    session.commit()

        task = asyncio.create_task(self._run(download_id, spec, state))
        self._tasks[download_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(download_id, None))
        return True

    def set_priority(self, download_id: int, priority: int) -> None:
        priority = max(-10, min(10, int(priority)))
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            download.priority = priority
            session.commit()
            snapshot = self.snapshot_item(download)
        self.core.set_priority(download_id, priority)
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        self._advance_queues_for(download_id)

    def pause(self, download_id: int) -> None:
        if download_id in self._stream_cancel:
            self._stream_cancel[download_id].set()
            self._update_db_status(download_id, DownloadStatus.paused)
            return
        self.core.pause(download_id)
        self._update_db_status(download_id, DownloadStatus.paused)

    def resume(self, download_id: int) -> None:
        if download_id in self._stream_cancel:
            # A streaming download is still winding down from pause (the yt-dlp
            # thread has not yet observed the cancel event). Restarting now
            # races the task teardown, so retry once it has fully stopped.
            self._schedule_stream_restart(download_id)
            return
        task = self.core.get(download_id)
        if task is not None:
            self.core.resume(download_id)
            self._update_db_status(download_id, DownloadStatus.downloading)
        else:
            self.start(download_id)

    def _schedule_stream_restart(self, download_id: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _retry() -> None:
            while (
                download_id in self._stream_cancel
                or download_id in self._tasks
            ):
                await asyncio.sleep(0.1)
            self.start(download_id)

        loop.create_task(_retry())

    def cancel(self, download_id: int, *, remove_file: bool = False) -> None:
        if download_id in self._stream_cancel:
            self._stream_cancel[download_id].set()
            if remove_file:
                self._delete_parts(download_id)
            return
        self.core.cancel(download_id)
        if remove_file:
            self._delete_parts(download_id)

    async def restart(self, download_id: int) -> bool:
        """Cancel any active work, discard partial data, and start fresh."""
        task = self._tasks.get(download_id)
        if task is not None:
            if download_id in self._stream_cancel:
                self._stream_cancel[download_id].set()
            else:
                self.core.cancel(download_id)
            await asyncio.gather(task, return_exceptions=True)
        self._delete_parts(download_id)
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return False
            download.status = DownloadStatus.queued
            download.error = None
            download.retry_count = 0
            download.size_downloaded = 0
            download.completed_at = None
            session.commit()
        self.events.post(Events.DOWNLOAD_UPDATED, self.snapshot_item(download))
        return self.start(download_id)

    def remove(self, download_id: int, *, delete_file: bool = False) -> None:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            if download_id in self._tasks:
                if download_id in self._stream_cancel:
                    self._stream_cancel[download_id].set()
                else:
                    self.core.cancel(download_id)
            self._delete_parts(download_id)
            if delete_file and download.save_path:
                try:
                    Path(download.save_path).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("file_delete_failed", error=str(exc))
            repo.remove(download)
        self.events.post(Events.DOWNLOAD_REMOVED, {"id": download_id})
        self._advance_queues_for(download_id)

    def start_all(self) -> None:
        with self.session_factory() as session:
            downloads = DownloadRepository(session).list(limit=2000)
        for download in downloads:
            if download.status in (DownloadStatus.queued, DownloadStatus.scheduled):
                self.start(download.id)

    def count_active_in_queue(self, queue_id: int) -> int:
        with self.session_factory() as session:
            repo = QueueRepository(session)
            items = repo.items(queue_id)
        return sum(1 for item in items if self._is_active(item.download_id))

    def _is_active(self, download_id: int) -> bool:
        task = self.core.get(download_id)
        if task is not None:
            return not task.is_terminal()
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        return bool(download and download.status in ACTIVE_STATUSES)

    # ----- execution -----

    async def _run(
        self,
        download_id: int,
        spec: Any,
        state: MClipState | None,
    ) -> None:
        async with self.semaphore:
            task = self.core.submit(spec, state)
            try:
                result = await task.run()
                self._finalize(download_id, result, task)
            except asyncio.CancelledError:
                self.core.cancel(download_id)
                self._update_db_status(download_id, DownloadStatus.stopped)
                raise
            finally:
                self.core.remove_task(download_id)

    # ----- streaming (yt-dlp) execution -----

    def _start_streaming(self, download_id: int) -> bool:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is not None and download.status not in (
                DownloadStatus.paused,
                DownloadStatus.queued,
                DownloadStatus.scheduled,
            ):
                download.status = DownloadStatus.scheduled
                session.commit()
        task = asyncio.create_task(self._run_streaming(download_id))
        self._tasks[download_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(download_id, None))
        return True

    async def _run_streaming(self, download_id: int) -> None:
        """Download an embedded/streaming URL with yt-dlp, mirroring events into
        the normal progress/state pipeline so the DB and UI stay in sync."""
        async with self.semaphore:
            cancel_event = threading.Event()
            self._stream_cancel[download_id] = cancel_event
            try:
                with self.session_factory() as session:
                    download = DownloadRepository(session).get(download_id)
                    if download is None:
                        return
                    url = download.url
                    existing_path = (
                        Path(download.save_path) if download.save_path else None
                    )
                    save_dir = (
                        existing_path.parent
                        if existing_path is not None
                        else Path(self.settings.get("downloads.default_directory", ""))
                    )
                    save_dir.mkdir(parents=True, exist_ok=True)
                    cookies = self._stream_cookies(download)
                quality = str(
                    self.settings.get("streaming.quality", "best") or "best"
                )

                self.events.post(
                    Events.DOWNLOAD_STATE_CHANGED,
                    {"id": download_id, "state": "connecting"},
                )

                # Resume: when a previous run left a partial file behind, reuse
                # the stored filename so yt-dlp continues appending to the same
                # ``.part`` file instead of re-resolving the stream title (which
                # could change) and starting the download over. Merged/DASH
                # downloads leave per-format intermediates such as
                # ``name.f137.mp4.part``/``name.f140.m4a.part`` (and no plain
                # ``name.part``), so the glob below covers every yt-dlp part
                # shape (single ``.part``, old ``.partN``, ``.part-FragN`` and
                # per-format ``.fXXX`` parts).
                partial = existing_path is not None and (
                    existing_path.exists()
                    or any(
                        True
                        for pattern in (
                            existing_path.name + ".part*",
                            existing_path.name + "*.part",
                        )
                        for _ in existing_path.parent.glob(pattern)
                    )
                )
                if partial:
                    info = None
                    filename = existing_path.name
                else:
                    info = await asyncio.to_thread(
                        resolve_stream, url, quality, cookies=cookies
                    )
                    filename = f"{info.title}.{info.ext}"

                with self.session_factory() as session:
                    repo = DownloadRepository(session)
                    download = repo.get(download_id)
                    if download is None:
                        return
                    download.filename = filename
                    download.save_path = str(save_dir / filename)
                    if info is not None:
                        download.detected_type = info.media_type
                        if info.size:
                            download.size_total = info.size
                    session.commit()

                self.events.post(
                    Events.DOWNLOAD_STATE_CHANGED,
                    {"id": download_id, "state": "downloading"},
                )

                final_path = await asyncio.to_thread(
                    download_stream,
                    url,
                    save_dir,
                    quality,
                    self._make_stream_progress(download_id),
                    cancel_event,
                    cookies=cookies,
                    filename=filename,
                )

                self._finalize_stream(download_id, "completed", final_path)
            except DownloadCancelled:
                if not self._db_status_is(download_id, DownloadStatus.paused):
                    self._update_db_status(download_id, DownloadStatus.stopped)
            except StreamResolutionError as exc:
                self._fail_stream(download_id, f"stream resolution failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - stream pipeline failure
                log.warning("stream_download_failed", id=download_id, error=str(exc))
                self._fail_stream(download_id, f"stream download failed: {exc}")
            finally:
                self._stream_cancel.pop(download_id, None)

    @staticmethod
    def _stream_cookies(download) -> dict[str, str] | None:
        """Pull the browser ``Cookie`` header stored on a streaming download."""
        headers = dict(download.headers_json or {})
        cookie = headers.pop("cookie", None)
        if not cookie:
            return None
        from ...network.cookies.jar import parse_cookie_header

        return parse_cookie_header(cookie) or None

    def _make_stream_progress(self, download_id: int):
        def _hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                self.events.post(
                    Events.PROGRESS_UPDATED,
                    {
                        "id": download_id,
                        "downloaded": int(d.get("downloaded_bytes") or 0),
                        "total": int(
                            d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        ),
                    },
                )
                speed = d.get("speed")
                if speed is not None:
                    self.events.post(
                        Events.SPEED_UPDATED,
                        {"id": download_id, "speed": float(speed)},
                    )
                eta = d.get("eta")
                if eta is not None:
                    self._etas[download_id] = float(eta)

        return _hook

    def _fail_stream(self, download_id: int, error: str) -> None:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            download.status = DownloadStatus.failed
            download.error = error
            session.commit()
            snapshot = self.snapshot_item(download)
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        self._post_notification("failed", snapshot)
        self._advance_queues_for(download_id)

    def _finalize_stream(self, download_id: int, result: str, final_path: Path) -> None:
        try:
            size = final_path.stat().st_size if final_path.exists() else 0
        except OSError:
            size = 0
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            download.status = DownloadStatus.completed
            download.save_path = str(final_path)
            download.filename = final_path.name
            download.size_total = size
            download.size_downloaded = size
            download.completed_at = _now()
            session.commit()
            download = repo.get(download_id)
            snapshot = self.snapshot_item(download) if download else {"id": download_id}
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        self._post_notification("completed", snapshot)
        self._advance_queues_for(download_id)
        self._inspect_media_async(download_id)

    def _db_status_is(self, download_id: int, status: DownloadStatus) -> bool:
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        return bool(download and download.status == status)

    def _finalize(self, download_id: int, result: str | None, task: Any) -> None:
        state = task.state.state
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            if result == "completed":
                download.status = DownloadStatus.completed
                download.size_downloaded = task.state.bytes_downloaded
                download.size_total = task.state.total_size or download.size_total
                download.hash_calculated = task.state.hash_calculated
                download.completed_at = _now()
                self._inspect_media_async(download_id)
            elif result == "verification_failed":
                download.status = DownloadStatus.verification_failed
                download.error = task.error or "integrity check failed"
            elif result == "failed":
                download.status = DownloadStatus.failed
                download.error = task.error
            elif result == "stopped":
                download.status = DownloadStatus.stopped
            elif state in ("paused", "queued"):
                download.status = DownloadStatus.paused
            session.commit()
            download = repo.get(download_id)
            snapshot = self.snapshot_item(download) if download else {"id": download_id}
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        self._advance_queues_for(download_id)
        self._post_notification(result, snapshot)

    def _post_notification(self, result: str | None, snapshot: dict) -> None:
        title = snapshot.get("filename") or f"Download #{snapshot.get('id')}"
        if result == "completed":
            kind = "completed"
            body = "Download complete"
        elif result == "verification_failed":
            kind = "failed"
            body = "Integrity verification failed"
        elif result == "failed":
            kind = "failed"
            body = "Download failed"
        else:
            return
        self.events.post(
            Events.NOTIFICATION_REQUESTED,
            {
                "kind": kind,
                "title": title,
                "body": body,
                "download_id": snapshot.get("id"),
            },
        )

    # ----- media inspection -----

    def _inspect_media_async(self, download_id: int) -> None:
        try:
            asyncio.create_task(self._inspect_media(download_id))
        except RuntimeError:
            # No running loop (e.g. host mode); inspection is best-effort.
            pass

    async def _inspect_media(self, download_id: int) -> None:
        from ...media.detect import detect_type
        from ...media.ffmpeg import FFmpegLocator, probe
        from ...media.metadata import extract_from_filename

        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
            if download is None or not download.save_path:
                return
            filename = download.filename or Path(download.save_path).name
            detected = detect_type(filename=filename, url=download.url)

        metadata: dict[str, Any] = {}
        if detected in ("video", "audio"):
            locator = FFmpegLocator()
            if locator.available:
                info = await probe(download.save_path, locator)
                if info:
                    metadata.update(info)
        if not metadata:
            metadata = extract_from_filename(filename)

        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
            if download is None:
                return
            download.detected_type = detected
            if metadata:
                download.media_metadata_json = metadata
            session.commit()
            snapshot = self.snapshot_item(download)
        self.events.post(Events.MEDIA_DETECTED, {**snapshot, "media": metadata})

    # ----- engine event mirrors -----

    def _on_state_changed(self, payload: dict) -> None:
        state = payload.get("state")
        # Terminal states are persisted by ``_finalize`` (the single writer),
        # so the DB never observes a terminal status before sizes are written.
        mapping = {
            "connecting": DownloadStatus.connecting,
            "downloading": DownloadStatus.downloading,
            "retrying": DownloadStatus.retrying,
            "verifying": DownloadStatus.verifying,
            "paused": DownloadStatus.paused,
        }
        status = mapping.get(state)
        if status is None:
            return
        fields: dict[str, Any] = {}
        if status is DownloadStatus.downloading:
            fields["started_at"] = _now()
        if payload.get("error"):
            fields["error"] = payload["error"]
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(payload["id"])
            if download is None:
                return
            if download.started_at is not None and "started_at" in fields:
                del fields["started_at"]
            for key, value in fields.items():
                setattr(download, key, value)
            download.status = status
            session.commit()
            snapshot = self.snapshot_item(download)
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)

    def _on_progress(self, payload: dict) -> None:
        now = time.monotonic()
        download_id = payload["id"]
        if now - self._last_progress_write.get(download_id, 0.0) < 1.0:
            return
        self._last_progress_write[download_id] = now
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            download.size_downloaded = int(payload.get("downloaded") or 0)
            if payload.get("total"):
                download.size_total = int(payload["total"])
            if payload.get("speed") is not None:
                speed = float(payload["speed"])
                download.speed_avg = speed
                download.speed_peak = max(download.speed_peak or 0.0, speed)
            session.commit()

    def _on_speed(self, payload: dict) -> None:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(payload["id"])
            if download is None:
                return
            speed = float(payload.get("speed") or 0.0)
            ema = self._predictor.update(speed)
            download.speed_avg = ema
            download.speed_peak = max(download.speed_peak or 0.0, speed)
            remaining = (download.size_total or 0) - (download.size_downloaded or 0)
            self._etas[download.id] = self._predictor.eta(remaining)
            session.commit()

    def _on_connections(self, payload: dict) -> None:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(payload["id"])
            if download is None:
                return
            download.connections_active = int(payload.get("active") or 0)
            download.connections_max = int(payload.get("max") or download.connections_max)
            session.commit()

    def _on_network_changed(self, payload) -> None:
        override = None
        if isinstance(payload, dict):
            override = payload.get("bandwidth_bytes_per_second")
        self._apply_bandwidth(override)

    def _on_settings_changed(self, payload: dict) -> None:
        simultaneous = int(self.settings.get("downloads.simultaneous", 3))
        if self.semaphore._value != simultaneous:
            self.semaphore = asyncio.Semaphore(simultaneous)
        self._apply_bandwidth()

    # ----- helpers -----

    def _apply_bandwidth(self, override: float | None = None) -> None:
        if override is not None:
            self.core.set_bandwidth(override)
            return
        mbps = float(self.settings.get("network.max_bandwidth_mbps", 0.0) or 0.0)
        self.core.set_bandwidth(mbps * 1024 * 1024)

    def _build_spec(self, download: Download) -> Any:
        save_dir = Path(download.save_path).parent if download.save_path else Path.home()
        save_dir.mkdir(parents=True, exist_ok=True)
        headers = dict(download.headers_json or {})
        cookies = None
        if "cookie" in headers:
            from ...network.cookies.jar import parse_cookie_header

            cookies = parse_cookie_header(headers.pop("cookie"))
        return spec_from_url(
            download_id=download.id,
            url=download.url,
            save_dir=save_dir,
            filename=download.filename or Path(download.save_path).name,
            headers=headers,
            auth=self._build_auth(download.auth_ref),
            proxy=self._build_proxy(download.proxy_profile_id),
            cookies=cookies,
            connections_max=download.connections_max,
            retry_max=int(self.settings.get("network.retry_max", 5)),
            timeout=float(self.settings.get("network.timeout_seconds", 30)),
            hash_algo=download.hash_algo,
            hash_expected=download.hash_expected,
            user_agent=str(self.settings.get("network.user_agent", "MagnetoClip/0.1")),
            priority=download.priority or 0,
        )

    def _build_auth(self, auth_ref: str | None):
        if not auth_ref:
            return None
        from ...network.auth.credentials import AuthSpec, get_secret

        password = get_secret(auth_ref)
        return AuthSpec(type="basic", username=auth_ref, password=password)

    def _build_proxy(self, proxy_profile_id: int | None):
        proxies = getattr(self.context, "proxies", None)
        if proxies is None:
            return None
        return proxies.to_spec(proxy_profile_id)

    @staticmethod
    def _merge_cookie_header(
        headers: dict[str, str] | None, cookies: dict[str, str] | None
    ) -> dict[str, str] | None:
        if not cookies:
            return dict(headers) if headers else None
        from ...network.cookies.jar import format_cookie_header

        merged = dict(headers or {})
        existing = merged.pop("cookie", None)
        all_cookies = dict(cookies)
        if existing:
            all_cookies.update(
                dict(
                    pair.split("=", 1)
                    for pair in existing.split(";")
                    if "=" in pair
                )
            )
        merged["cookie"] = format_cookie_header(all_cookies)
        return merged

    @staticmethod
    def _store_auth_ref(username: str | None, password: str | None) -> str | None:
        if not username:
            return None
        from ...network.auth.credentials import set_secret

        set_secret(username, password or "")
        return username

    def _load_resume_state(self, spec: Any) -> MClipState | None:
        sidecar = MClipState.sidecar_for(str(spec.final_path))
        if not sidecar.exists():
            return None
        try:
            state = MClipState.load(sidecar)
        except Exception:  # noqa: BLE001 - corrupt sidecar, start over
            log.warning("mclip_corrupt_ignored", path=str(sidecar))
            return None
        if state.state in ("completed", "verification_failed"):
            return None
        if state.url != spec.url:
            return None
        state.state = "queued"
        state.hash_algo = state.hash_algo or spec.hash_algo
        state.hash_expected = state.hash_expected or spec.hash_expected
        return state

    def _sync_state_to_db(self, download_id: int, state: MClipState) -> None:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            download.size_total = state.total_size or download.size_total
            download.size_downloaded = state.bytes_downloaded
            if state.etag:
                download.etag = state.etag
            if state.last_modified:
                download.last_modified = state.last_modified
            if state.hash_calculated:
                download.hash_calculated = state.hash_calculated
            session.commit()

    def _delete_parts(self, download_id: int) -> None:
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        if download is None or not download.save_path:
            return
        final = Path(download.save_path)
        for suffix in (".mclip", ".part", ".ytdl"):
            try:
                Path(f"{final}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
        try:
            for part in final.parent.glob(f"{final.name}.part*"):
                part.unlink(missing_ok=True)
        except OSError:
            pass

    def _advance_queues_for(self, download_id: int) -> None:
        with self.session_factory() as session:
            queue_ids = QueueRepository(session).queue_ids_for_download(download_id)
        for queue_id in queue_ids:
            self.queues.advance(queue_id)

    @staticmethod
    def _validate_url(url: str) -> None:
        try:
            parsed = httpx.URL(url)
        except Exception as exc:
            raise ValueError("invalid URL") from exc
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise ValueError("only http/https URLs are supported")

    @staticmethod
    def _derive_name(url: str) -> str:
        """Derive a download filename from a URL.

        The query string is dropped so CDN URLs with huge ``_nc_*``/``stp``
        parameters (Facebook, Telegram) do not turn into absurdly long
        filenames. Extensionless CDN URLs that declare their format as a query
        parameter (``pbs.twimg.com/media/x?format=jpg``) keep a useful
        extension.
        """
        segment = (url or "").rsplit("/", 1)[-1]
        path_part = segment.split("?", 1)[0].split("#", 1)[0] or "download"
        if "." in path_part:
            return path_part
        match = re.search(r"[?&]format=([a-z0-9]{2,8})", segment, re.IGNORECASE)
        if match:
            return f"{path_part}.{match.group(1).lower()}"
        return path_part

    def _resolve_save_dir(self, save_dir: Path | str | None, category: Any) -> Path:
        if save_dir is not None:
            return Path(save_dir).expanduser()
        default = Path(self.settings.get("downloads.default_directory")).expanduser()
        if category is not None and category.folder:
            folder = Path(category.folder)
            if not folder.is_absolute():
                folder = default / folder
            return folder
        return default

    def _update_db_status(self, download_id: int, status: DownloadStatus) -> None:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.update_status(download_id, status)
            snapshot = self.snapshot_item(download) if download else None
        if snapshot:
            self.events.post(Events.DOWNLOAD_UPDATED, snapshot)

    # ----- views -----

    def snapshot_item(self, download: Download) -> dict[str, Any]:
        return {
            "id": download.id,
            "url": download.url,
            "filename": download.filename,
            "save_path": download.save_path,
            "size_total": download.size_total,
            "size_downloaded": download.size_downloaded,
            "status": download.status.value,
            "speed": download.speed_avg,
            "speed_peak": download.speed_peak,
            "eta_seconds": self._etas.get(download.id),
            "priority": download.priority,
            "connections_max": download.connections_max,
            "connections_active": download.connections_active,
            "hash_algo": download.hash_algo,
            "hash_expected": download.hash_expected,
            "hash_calculated": download.hash_calculated,
            "detected_type": download.detected_type,
            "media_metadata": download.media_metadata_json,
            "created_at": download.created_at.isoformat() if download.created_at else None,
            "started_at": download.started_at.isoformat() if download.started_at else None,
            "completed_at": download.completed_at.isoformat() if download.completed_at else None,
            "error": download.error,
            "category_id": download.category_id,
        }

    def list_snapshots(self, **filters: Any) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            downloads = DownloadRepository(session).list(**filters)
        return [self.snapshot_item(download) for download in downloads]

    def get_download(self, download_id: int) -> Download | None:
        with self.session_factory() as session:
            return DownloadRepository(session).get(download_id)

    def path_of(self, download_id: int) -> Path | None:
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        if download is None or not download.save_path:
            return None
        return Path(download.save_path)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.core.shutdown()


def _now():
    from datetime import datetime

    return datetime.now(UTC)
