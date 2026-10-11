"""DownloadManager: application-level orchestration over MagnetoCore.

Responsibilities:
- accept new downloads (URL validation, filename sanitization, auto-categorize)
- start/pause/resume/cancel/restart/remove downloads with concurrency limits
- resume interrupted downloads from ``.mclip`` sidecars
- mirror engine progress/state events into the SQLite database
- apply global bandwidth from settings
- advance the global torrent queue when capacity frees up
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
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ...app.context import AppContext
from ...database.models import Download, DownloadStatus, Playlist, PlaylistItem
from ...database.repositories import DownloadRepository, PendingCaptureRepository
from ...engine.downloader.engine import MagnetoCore, spec_from_url
from ...engine.resume.mclip import MClipState
from ...intelligence import SpeedPredictor
from ...media.models import RequestContext
from ...media.streaming import (
    DownloadCancelled,
    StreamResolutionError,
    download_stream,
    is_audio_platform,
    is_bot_wall_error,
    is_hls_or_dash_url,
    is_hls_url,
    is_streaming_site_url,
    is_streaming_url,
    resolve_stream,
)
from ...media.youtube.types import (
    PlaylistItemStatus,
    PlaylistVideo,
    YouTubePlaylist,
)
from ...network.http.client import ClientConfig
from ...security.safe_names import safe_join, sanitize_filename
from ...services.logging import get_logger
from ...torrent.client import TorrentClient
from ...torrent.client import available as torrent_available
from ...torrent.detect import is_magnet_link, is_torrent_file_url, is_torrent_url
from ...torrent.handler import TorrentDownloadHandler
from ...torrent.resume import sidecar_path_for
from ...torrent.types import TorrentSpec
from ..events.bus import Events
from ..torrent_queue import TorrentQueue

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
    ) -> None:
        self.context = context
        self.settings = context.settings
        self.session_factory: sessionmaker = context.session_factory
        self.events = context.events
        client_config = ClientConfig(
            user_agent=str(self.settings.get("network.user_agent", "MagnetoClip/0.1")),
            timeout=float(self.settings.get("network.timeout_seconds", 30)),
            verify_tls=bool(self.settings.get("network.verify_tls", True)),
        )
        self.core = core or MagnetoCore(bus=context.events, client_config=client_config)
        self.categories = categories or context.categories
        self.torrent_queue = TorrentQueue(self)

        self.semaphore = asyncio.Semaphore(
            int(self.settings.get("downloads.simultaneous", 3))
        )
        self._tasks: dict[int, asyncio.Task] = {}
        self._last_progress_write: dict[int, float] = {}
        self._predictor = SpeedPredictor()
        self._etas: dict[int, float | None] = {}
        self._stream_cancel: dict[int, threading.Event] = {}
        self._paused_by_switch: set[int] = set()
        self._torrent_handlers: dict[int, TorrentDownloadHandler] = {}
        self._pending_torrent_opts: dict[int, dict] = {}
        self._torrent_client: TorrentClient | None = None
        self._init_torrent_client()

        self.events.connect(Events.DOWNLOAD_STATE_CHANGED, self._on_state_changed)
        self.events.connect(Events.PROGRESS_UPDATED, self._on_progress)
        self.events.connect(Events.SPEED_UPDATED, self._on_speed)
        self.events.connect(Events.CONNECTIONS_UPDATED, self._on_connections)
        self.events.connect(Events.NETWORK_CHANGED, self._on_network_changed)
        self.events.connect(Events.SETTINGS_CHANGED, self._on_settings_changed)
        self.events.connect(Events.TORRENT_NAME_RESOLVED, self._on_torrent_name_resolved)
        self.events.connect(Events.PAUSE_STATE_CHANGED, self._on_pause_state_changed)

        self._apply_bandwidth()

    def _init_torrent_client(self) -> None:
        """Initialize the torrent client singleton if libtorrent is available."""
        if not torrent_available():
            log.info("torrent_unavailable")
            return
        # Reuse the session created during app startup so we don't spin up a
        # second session fighting for the same listen port / DHT pool.
        existing = getattr(self.context, "torrent_client", None)
        if existing is not None:
            self._torrent_client = existing
            return
        try:
            from ...torrent.client import ClientConfig as TConfig

            config = TConfig.from_settings(self.settings)
            self._torrent_client = TorrentClient(config)
            self.context.torrent_client = self._torrent_client
        except Exception as exc:
            log.warning("torrent_client_init_failed", error=str(exc))
            self._torrent_client = None

    # ----- creation -----

    def add(
        self,
        url: str,
        *,
        filename: str | None = None,
        save_dir: Path | str | None = None,
        category_name: str | None = None,
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
        data_path: Path | None = None,
        archive: bool = False,
        playlist_id: str | None = None,
        playlist_title: str | None = None,
        playlist_item_index: int | None = None,
        request_context=None,
        youtube_quality: str | None = None,
        youtube_height: int | None = None,
        youtube_width: int | None = None,
        youtube_fps: int | None = None,
        youtube_format: str | None = None,
        youtube_format_selector: str | None = None,
        youtube_merge_output_format: str | None = None,
        youtube_format_label: str | None = None,
        youtube_requested_quality: str | None = None,
    ) -> Download:
        """Validate the URL and persist a new download record.

        ``data`` lets a capture hand over in-memory bytes (e.g. a Telegram
        ``blob:`` image the extension fetched for us). ``data_path`` does the
        same for a file the browser already downloaded to disk (the Chrome
        downloads fallback for one-shot Telegram stream links): one of the two
        means no network request happens, the bytes are written straight to
        disk and the record is created as completed. The URL may then be a
        ``blob:`` URI or a non-fetchable session URL.
        """
        if data is None and data_path is None:
            self._validate_url(url)
        torrent = is_torrent_url(url)
        name = sanitize_filename(filename) if filename else self._derive_name(url)
        category = None
        if category_name:
            category = self.categories.get_by_name(category_name)
        if torrent and self.settings.get("downloads.auto_categorize", True):
            category = self.categories.get_by_name("Torrent")
        streaming = not torrent and is_streaming_url(url)
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

        substitute = self._dedup_on_add(
            url,
            name,
            str(final_path),
            data,
            category.id if category else None,
        )
        if substitute is not None:
            self.events.post(Events.DOWNLOAD_ADDED, self.snapshot_item(substitute))
            return substitute

        connections = connections_max or int(
            self.settings.get("downloads.connections_per_download", 16)
        )
        auth_ref = self._store_auth_ref(auth_username, auth_password)
        if proxy_profile_id is None:
            proxy_profile_id = int(
                self.settings.get("network.default_proxy_id", 0) or 0
            ) or None
        if data is not None:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(data)
        elif data_path is not None:
            import shutil

            final_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(data_path, final_path)
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.add(
                url,
                filename=final_path.name,
                save_path=str(final_path),
                category_id=category.id if category else None,
                priority=priority,
                connections_max=connections,
                headers=self._merge_cookie_header(headers, cookies),
                hash_algo=hash_algo,
                hash_expected=hash_expected,
                proxy_profile_id=proxy_profile_id,
                auth_ref=auth_ref,
                stream_context=(
                    request_context.to_dict()
                    if request_context is not None
                    else None
                ),
                playlist_id=playlist_id,
                playlist_title=playlist_title,
                playlist_item_index=playlist_item_index,
                youtube_quality=youtube_quality,
                youtube_height=youtube_height,
                youtube_width=youtube_width,
                youtube_fps=youtube_fps,
                youtube_format=youtube_format,
                youtube_format_selector=youtube_format_selector,
                youtube_merge_output_format=youtube_merge_output_format,
                youtube_format_label=youtube_format_label,
                youtube_requested_quality=youtube_requested_quality,
            )
            if data is not None:
                download.status = DownloadStatus.completed
                download.size_total = len(data)
                download.size_downloaded = len(data)
                download.completed_at = datetime.now(UTC)
            elif data_path is not None:
                download.status = DownloadStatus.completed
                download.size_total = int(final_path.stat().st_size)
                download.size_downloaded = download.size_total
                download.completed_at = datetime.now(UTC)
            from ...media.detect import detect_type

            if torrent:
                download.detected_type = "torrent"
                if is_magnet_link(url):
                    download.torrent_info_hash = ""
                download.torrent_sequential = bool(
                    self.settings.get("torrent.default_sequential", False)
                )
            elif stream_kind:
                download.detected_type = stream_kind
            else:
                download.detected_type = detect_type(filename=final_path.name, url=url)
            session.commit()
        self.events.post(Events.DOWNLOAD_ADDED, self.snapshot_item(download))
        if archive and data is None:
            download.detected_type = "webpage"
            download.save_path = str(final_path.parent / f"{Path(name).stem}.html")
            download.filename = Path(download.save_path).name
            with self.session_factory() as session:
                repo = DownloadRepository(session)
                d = repo.get(download.id)
                if d is not None:
                    d.detected_type = "webpage"
                    d.filename = download.filename
                    d.save_path = download.save_path
                    session.commit()
            self._run_archiving(download.id, url, final_path.parent)
            return download
        return download

    def add_playlist(
        self,
        playlist: YouTubePlaylist,
        videos: list[PlaylistVideo] | None = None,
        *,
        save_dir: Path | str | None = None,
        category_name: str | None = None,
        connections_max: int | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        youtube_quality: str | None = None,
    ) -> list[tuple[PlaylistVideo, Download]]:
        """Queue *selected* playlist videos as individual downloads.

        Each video becomes its own ``Download`` row through the normal
        single-video pipeline (streaming, progress events, notifications),
        grouped under one ``Playlist`` row and - when ``playlist.create_folder``
        is enabled - inside a ``<playlist title>`` folder. Returns
        ``(video, download)`` pairs in playlist order.
        """
        videos = list(videos if videos is not None else playlist.selected())
        if not videos:
            return []
        playlist_id = playlist.playlist_id
        playlist_title = playlist.title or playlist_id
        base_dir = self._resolve_save_dir(save_dir, None)
        if bool(self.settings.get("playlist.create_folder", True)):
            folder = sanitize_filename(playlist_title) or "playlist"
            base_dir = base_dir / folder
        base_dir.mkdir(parents=True, exist_ok=True)

        with self.session_factory() as session:
            playlist_row = session.scalar(
                select(Playlist).where(Playlist.playlist_id == playlist_id)
            )
            if playlist_row is None:
                playlist_row = Playlist(
                    playlist_id=playlist_id,
                    title=playlist.title,
                    url=playlist.watch_url(),
                    video_count=len(videos),
                )
                session.add(playlist_row)
                session.commit()

        created: list[tuple[PlaylistVideo, Download]] = []
        for video in videos:
            download = self.add(
                video.url,
                save_dir=base_dir,
                category_name=category_name,
                connections_max=connections_max,
                headers=headers,
                cookies=cookies,
                playlist_id=playlist_id,
                playlist_title=playlist_title,
                playlist_item_index=video.index,
                youtube_quality=youtube_quality,
                youtube_requested_quality=youtube_quality,
            )
            with self.session_factory() as session:
                item = session.scalar(
                    select(PlaylistItem).where(
                        PlaylistItem.playlist_id == playlist_row.id,
                        PlaylistItem.video_id == video.video_id,
                    )
                )
                if item is None:
                    item = PlaylistItem(
                        playlist_id=playlist_row.id,
                        video_id=video.video_id,
                        title=video.title,
                        url=video.url,
                        index=video.index,
                        duration=video.duration,
                        status="added",
                        download_id=download.id,
                        requested_quality=youtube_quality,
                    )
                    session.add(item)
                else:
                    item.download_id = download.id
                    item.status = "added"
                    item.error = None
                    item.title = video.title
                    item.duration = video.duration
                    if youtube_quality:
                        item.requested_quality = youtube_quality
                session.commit()
            video.status = PlaylistItemStatus.added
            created.append((video, download))
        return created

    def _sync_playlist_item(
        self, download: Download, status: str, error: str | None = None
    ) -> None:
        """Mirror a terminal state onto the ``PlaylistItem`` of a playlist video."""
        playlist_item_index = download.playlist_item_index
        if not download.playlist_id or playlist_item_index is None:
            return
        with self.session_factory() as session:
            item = session.scalar(
                select(PlaylistItem)
                .join(Playlist, PlaylistItem.playlist_id == Playlist.id)
                .where(
                    Playlist.playlist_id == download.playlist_id,
                    PlaylistItem.index == playlist_item_index,
                )
            )
            if item is None:
                return
            item.status = status
            item.error = error
            session.commit()

    def _sync_playlist_item(
        self, download: Download, status: str, error: str | None = None
    ) -> None:
        """Mirror a terminal state onto the ``PlaylistItem`` of a playlist video."""
        playlist_item_index = download.playlist_item_index
        if not download.playlist_id or playlist_item_index is None:
            return
        with self.session_factory() as session:
            item = session.scalar(
                select(PlaylistItem)
                .join(Playlist, PlaylistItem.playlist_id == Playlist.id)
                .where(
                    Playlist.playlist_id == download.playlist_id,
                    PlaylistItem.index == playlist_item_index,
                )
            )
            if item is None:
                return
            item.status = status
            item.error = error
            video_id = item.video_id
            session.commit()
        if status == "completed" and video_id:
            self._playlist_archive().add(video_id)

    def _sync_playlist_actual_quality(
        self, download: Download, height: int | None
    ) -> None:
        """Mirror a download's post-probe actual resolution onto its item."""
        if not download.playlist_id or download.playlist_item_index is None:
            return
        if not height:
            return
        from ...media.youtube.formats import display_height

        label = display_height(height)
        with self.session_factory() as session:
            item = session.scalar(
                select(PlaylistItem)
                .join(Playlist, PlaylistItem.playlist_id == Playlist.id)
                .where(
                    Playlist.playlist_id == download.playlist_id,
                    PlaylistItem.index == download.playlist_item_index,
                )
            )
            if item is None:
                return
            item.actual_quality = label
            session.commit()

    def _playlist_archive(self) -> Any:
        """The persistent YouTube video-id archive under the app data dir."""
        from ...media.youtube.archive import PlaylistArchive

        archive = getattr(self, "_playlist_archive_cache", None)
        if archive is None:
            archive = PlaylistArchive(
                self.context.data_dir / "archives" / "youtube.txt"
            )
            self._playlist_archive_cache = archive
        return archive

    def archived_youtube_ids(self, limit: int = 10_000) -> set[str]:
        """Video ids already in the YouTube archive (used to pre-skip previews)."""
        return self._playlist_archive().ids()

    def _run_archiving(self, download_id: int, url: str, save_dir: Path) -> None:
        """Archive a webpage URL for the given download (off-thread)."""
        import threading

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running asyncio loop in this thread yet (e.g. a headless or
            # test caller); run the archive on a dedicated worker thread so the
            # download is still finalized instead of being left queued forever.
            threading.Thread(
                target=asyncio.run,
                args=(self._archive_async(download_id, url, save_dir),),
                daemon=True,
                name="webpage-archive",
            ).start()
            return
        asyncio.create_task(self._archive_async(download_id, url, save_dir))

    async def _archive_async(self, download_id: int, url: str, save_dir: Path) -> None:
        archiver = getattr(self.context, "archiver", None)
        if archiver is None:
            return

        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
            if download is None:
                return
            headers = dict(download.headers_json or {})
            self._post_notification("started", self.snapshot_item(download))
        result = await asyncio.to_thread(
            archiver.archive, url, save_dir, headers=headers
        )
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            if result.ok:
                download.status = DownloadStatus.completed
                download.save_path = result.path
                download.filename = Path(result.path).name
                download.completed_at = datetime.now(UTC)
                try:
                    download.size_total = Path(result.path).stat().st_size
                    download.size_downloaded = download.size_total
                except OSError:
                    pass
            else:
                download.status = DownloadStatus.failed
                download.error = result.error or "archive failed"
            session.commit()
            snapshot = self.snapshot_item(download)
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        self._post_notification(
            "completed" if result.ok else "failed", snapshot
        )

    # ----- duplicate detection -----

    def _dedup_on_add(
        self,
        url: str,
        filename: str,
        save_path: str,
        data: bytes | None,
        category_id: int | None,
    ) -> Download | None:
        """Resolve a duplicate before creating a new download.

        Returns an existing download to substitute (skip / open-existing) or
        ``None`` to proceed with a normal new download.

        * For ``data`` (capture) adds the content is known, so we hash it and
          compare against the index (an exact duplicate).
        * For URL adds we fall back to checking whether a completed download for
          the same URL, or the same resolved save path, already exists.

        The interactive "\u201cduplicate detected\u201d" decision is delegated to
        ``context.dedup_decision`` when present (UI add flows); otherwise the
        ``dedup.auto_skip`` policy decides. This keeps the method non-blocking
        for automated/headless paths (captures, remote, tests).
        """
        dedup = getattr(self.context, "dedup", None)
        if dedup is None or not self.settings.get("dedup.enabled", True):
            return None

        if data is not None:
            result = dedup.check_bytes(data, filename=filename)
        else:
            result = self._find_url_duplicate(url, save_path, filename)
        if result is None or not result.is_duplicate:
            return None

        payload = {
            "filename": filename,
            "url": url,
            "save_path": save_path,
            "existing_path": result.existing_path,
            "existing_filename": result.existing_filename,
            "existing_size": result.existing_size,
            "existing_categories": result.existing_categories,
        }
        self.events.post(Events.DUPLICATE_DETECTED, payload)

        decision = self._dedup_decision(payload)
        if decision == "download":
            return None
        # Skip or open-existing: substitute an existing record for the file.
        return self._existing_download_for(
            payload, url, filename, save_path, category_id
        )

    def _dedup_decision(self, payload: dict) -> str:
        """Decide the duplicate action: ``download``, ``skip``, or ``open``.

        When ``dedup.auto_skip`` is set we never prompt (automated path).
        Otherwise a UI decision hook (set by interactive add flows) is given
        the chance to show the ``DuplicateResultDialog``; if no hook is present
        we simply proceed with the download.
        """
        if self.settings.get("dedup.auto_skip", False):
            return "skip"
        hook = getattr(self.context, "dedup_decision", None)
        if hook is not None and callable(hook):
            try:
                return str(hook(payload) or "download")
            except Exception:
                return "download"
        return "download"

    def _find_url_duplicate(
        self,
        url: str,
        save_path: str,
        filename: str,
    ) -> Any | None:
        from ...core.dedup.types import DuplicateResult

        with self.session_factory() as session:
            repo = DownloadRepository(session)
            # A completed download already at the target path.
            if Path(save_path).is_file():
                return DuplicateResult(
                    is_duplicate=True,
                    size_match=True,
                    existing_path=save_path,
                    existing_filename=filename,
                )
            # A completed download for the same URL.
            for existing in repo.list(status=DownloadStatus.completed, limit=2000):
                if existing.url == url:
                    return DuplicateResult(
                        is_duplicate=True,
                        size_match=True,
                        existing_path=existing.save_path,
                        existing_filename=existing.filename,
                    )
        return None

    def _existing_download_for(
        self,
        payload: dict,
        url: str,
        filename: str,
        save_path: str,
        category_id: int | None,
    ) -> Download:
        """Find an existing completed ``Download`` row for the duplicated file,
        creating one pointing at the already-present file if none exists."""
        existing_path = payload.get("existing_path") or save_path
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            for existing in repo.list(limit=2000):
                if existing.save_path and Path(existing.save_path) == Path(
                    existing_path
                ):
                    return existing
            # No row yet: create a completed record that references the file.
            size = payload.get("existing_size") or 0
            try:
                size = Path(existing_path).stat().st_size
            except OSError:
                pass
            download = repo.add(
                url,
                filename=filename,
                save_path=str(existing_path),
                category_id=category_id,
            )
            download.status = DownloadStatus.completed
            download.size_total = size
            download.size_downloaded = size
            download.completed_at = datetime.now(UTC)
            session.commit()
            return download

    # ----- lifecycle -----

    def start(self, download_id: int, *, queue_advance: bool = False) -> bool:
        """Kick off ``download_id``; returns False if it is already running."""
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        if download is None:
            return False

        if is_torrent_url(download.url) or download.detected_type == "torrent":
            handler = self._torrent_handlers.get(download_id)
            if handler is not None and download.status in (
                DownloadStatus.paused,
                DownloadStatus.queued,
                DownloadStatus.scheduled,
            ):
                # Parked by pause(): the polling task is still alive, so a
                # plain restart would duplicate it. Resume in place instead.
                try:
                    handler.resume()
                except Exception as exc:
                    log.warning(
                        "torrent_resume_handler_error",
                        download_id=download_id,
                        error=str(exc),
                    )
                self._update_db_status(download_id, DownloadStatus.downloading)
                return True
            if (
                download.status in ACTIVE_STATUSES
                or download_id in self._tasks
                or download.status == DownloadStatus.completed
            ):
                return False
            if not queue_advance:
                # Manual starts respect the global torrent queue: torrents
                # waiting for admission (scheduled) or for a free transfer
                # slot stay queued until the queue advances them.
                if download.status == DownloadStatus.scheduled:
                    return False
                if self.torrent_queue.slots_full():
                    return False
            return self._start_torrent(download_id)

        if (
            download.status in ACTIVE_STATUSES
            or download_id in self._tasks
            or download.status == DownloadStatus.completed
        ):
            return False

        if is_streaming_url(download.url):
            log.info(
                "stream_start_dispatch",
                download_id=download_id,
                semaphore_available=getattr(self.semaphore, "_value", None),
                active_tasks=len(self._tasks),
            )
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
        self.torrent_queue.admit_and_advance()

    def pause(self, download_id: int) -> None:
        if download_id in self._stream_cancel:
            self._stream_cancel[download_id].set()
            self._update_db_status(download_id, DownloadStatus.paused)
            return
        if download_id in self._torrent_handlers:
            handler = self._torrent_handlers.get(download_id)
            if handler is not None:
                try:
                    handler.pause()
                except Exception as exc:
                    log.warning("torrent_pause_handler_error", download_id=download_id, error=str(exc))
            self._update_db_status(download_id, DownloadStatus.paused)
            # A paused torrent frees its transfer slot; let waiting
            # queued torrents use it right away.
            self.torrent_queue.advance()
            return
        self.core.pause(download_id)
        self._update_db_status(download_id, DownloadStatus.paused)

    def resume(self, download_id: int) -> None:
        if download_id in self._stream_cancel:
            self._schedule_stream_restart(download_id)
            return
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        if download is not None and (
            is_torrent_url(download.url) or download.detected_type == "torrent"
        ):
            self.torrent_queue.resume(download_id)
            return
        task = self.core.get(download_id)
        if task is not None:
            self.core.resume(download_id)
            self._update_db_status(download_id, DownloadStatus.downloading)
        else:
            self.start(download_id)

    def start_seeding(self, download_id: int) -> None:
        """Resume a completed torrent for seeding."""
        try:
            with self.session_factory() as session:
                repo = DownloadRepository(session)
                download = repo.get(download_id)
                if download is None:
                    return
                download.status = DownloadStatus.downloading
                download.torrent_seeding = True
                session.commit()
            # Seeding is not a queue-managed transfer; bypass slot limits.
            self.start(download_id, queue_advance=True)
        except Exception as exc:
            log.warning("torrent_start_seeding_error", download_id=download_id, error=str(exc))

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
        if download_id in self._torrent_handlers:
            handler = self._torrent_handlers.get(download_id)
            if handler is not None:
                handler.cancel()
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
            elif download_id in self._torrent_handlers:
                handler = self._torrent_handlers.get(download_id)
                if handler is not None:
                    handler.cancel()
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
                elif download_id in self._torrent_handlers:
                    handler = self._torrent_handlers.get(download_id)
                    if handler is not None:
                        handler.cancel()
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
        self.torrent_queue.admit_and_advance()

    def start_all(self) -> None:
        with self.session_factory() as session:
            downloads = DownloadRepository(session).list(limit=2000)
        for download in downloads:
            if download.status in (DownloadStatus.queued, DownloadStatus.scheduled):
                self.start(download.id)

    def _on_pause_state_changed(self, payload) -> None:
        """Pause or resume every download when the global switch flips."""
        if payload.get("paused"):
            self.pause_all()
        else:
            self.resume_all()

    def pause_all(self) -> None:
        """Pause every running or waiting download (toggled by the switch)."""
        with self.session_factory() as session:
            downloads = DownloadRepository(session).list(limit=2000)
        for download in downloads:
            if download.status in ACTIVE_STATUSES or download.status in (
                DownloadStatus.queued,
                DownloadStatus.scheduled,
            ):
                self._paused_by_switch.add(download.id)
                self.pause(download.id)

    def resume_all(self) -> None:
        """Resume the downloads that were paused by :meth:`pause_all`."""
        download_ids = list(self._paused_by_switch)
        self._paused_by_switch.clear()
        for download_id in download_ids:
            try:
                self.resume(download_id)
            except Exception:
                log.warning("resume_all_failed", id=download_id, exc_info=True)

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
        log.info(
            "stream_start_requested",
            download_id=download_id,
            semaphore_available=getattr(self.semaphore, "_value", None),
            active_tasks=len(self._tasks),
        )
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
        log.info(
            "stream_task_created",
            download_id=download_id,
            semaphore_available=getattr(self.semaphore, "_value", None),
        )
        task.add_done_callback(lambda _: self._tasks.pop(download_id, None))
        return True

    async def _run_streaming(self, download_id: int) -> None:
        """Download an embedded/streaming URL with yt-dlp, mirroring events into
        the normal progress/state pipeline so the DB and UI stay in sync."""
        log.info(
            "stream_waiting_for_slot",
            download_id=download_id,
            semaphore_available=getattr(self.semaphore, "_value", None),
        )
        async with self.semaphore:
            log.info(
                "stream_slot_acquired",
                download_id=download_id,
                semaphore_available=getattr(self.semaphore, "_value", None),
            )
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
                    headers = self._stream_headers(download)
                quality = str(
                    self.settings.get("streaming.quality", "best") or "best"
                )
                # Per-download YouTube selection (chosen against the real ladder
                # in the quality dialog) overrides the global quality setting.
                requested_quality = None
                format_selector = None
                merge_output_format = None
                with self.session_factory() as session:
                    row = DownloadRepository(session).get(download_id)
                    if row is not None and row.youtube_format_selector:
                        format_selector = row.youtube_format_selector
                        merge_output_format = (
                            row.youtube_merge_output_format or None
                        )
                        requested_quality = (
                            row.youtube_requested_quality or None
                        )
                    elif row is not None and row.youtube_requested_quality:
                        # Playlist / settings-default path: no per-video ladder
                        # was resolved, so build the standalone selector. The
                        # ``[height<=N]`` rule gives lower-if-unavailable.
                        from ...media.youtube.formats import (
                            build_standalone_selector,
                        )

                        requested_quality = row.youtube_requested_quality
                        format_selector = build_standalone_selector(
                            requested_quality
                        )
                        if "+" in format_selector:
                            merge_output_format = "mp4"
                browser_assist = bool(
                    self.settings.get("streaming.browser_assist", False)
                )

                self.events.post(
                    Events.DOWNLOAD_STATE_CHANGED,
                    {"id": download_id, "state": "connecting"},
                )

                # Browser-assisted HLS: streaming-site CDNs sit behind
                # Cloudflare "Managed Challenge", which no HTTP library passes.
                # When the extension is connected, fetch manifests/segments from
                # Chrome itself (which clears the challenge naturally), falling
                # back to yt-dlp on any pre-download failure so non-Cloudflare
                # manifest hosts keep working without the browser.
                # NOTE: yt-dlp with impersonation is tried first. Browser-assist
                # is used as a fallback when yt-dlp hits any Cloudflare block.
                # The challenge frequently answers with HTTP 200 + an HTML
                # challenge page, so yt-dlp reports "Unsupported URL"/"no video
                # formats" rather than an obvious 403 -- no bot-wall token in
                # the message. Fall back to the browser on ANY manifest-URL
                # failure (that path returns None when the browser cannot help,
                # so plain non-Cloudflare m3u8 hosts stay on yt-dlp).

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
                if (
                    not partial
                    and bool(self.settings.get("streaming.page_sniff", True))
                    and is_streaming_site_url(url)
                    and not is_hls_or_dash_url(url)
                ):
                    # In-app page → manifest sniffing: when a streaming-site page
                    # URL is pasted directly, fetch its HTML in-process and scan
                    # for an embedded .m3u8/.mpd reference so the HLS engine (or
                    # yt-dlp) can start on the manifest immediately. When the page
                    # is behind a Cloudflare challenge the scan finds nothing and
                    # we fall through to the yt-dlp + browser-assist paths as
                    # before. The page URL becomes the Referer so tokenized CDNs
                    # accept the subsequent manifest/segment requests.
                    sniffed = await self._sniff_stream_page(url, headers)
                    if sniffed is not None:
                        log.info(
                            "page_sniff_resolved",
                            id=download_id,
                            url=url,
                            manifest=sniffed.manifest_url,
                        )
                        url = sniffed.manifest_url
                        headers = dict(headers or {})
                        headers.setdefault(
                            "Referer", sniffed.referer or sniffed.page_url
                        )

                # HLS-first path: once an actual M3U8 has been discovered,
                # prefer the dedicated browser-assisted HLS engine. yt-dlp is
                # the fallback, not the primary transport. This matters for
                # streaming-site CDNs whose segment URLs are valid in Chrome
                # but rejected when replayed by a generic HTTP client.
                if (
                    browser_assist
                    and is_hls_url(url)
                    and not partial
                ):
                    log.info(
                        "hls_engine_start",
                        id=download_id,
                        url=url,
                        quality=quality,
                    )
                    hls_path = await self._run_browser_stream(
                        download_id,
                        url,
                        save_dir,
                        quality,
                        headers,
                        cancel_event,
                        existing_path,
                    )
                    if hls_path is not None:
                        self._finalize_stream(download_id, "completed", hls_path)
                        return

                if partial:
                    info = None
                    filename = existing_path.name
                else:
                    resolve_kwargs = dict(cookies=cookies, headers=headers)
                    if format_selector:
                        resolve_kwargs["format_selector"] = format_selector
                    if merge_output_format:
                        resolve_kwargs["merge_output_format"] = merge_output_format
                    info = await asyncio.to_thread(
                        resolve_stream, url, quality, **resolve_kwargs
                    )
                    filename = f"{info.title}.{info.ext}"

                with self.session_factory() as session:
                    repo = DownloadRepository(session)
                    download = repo.get(download_id)
                    if download is None:
                        return
                    if (
                        info is not None
                        and download.playlist_id
                        and download.playlist_item_index is not None
                    ):
                        filename = (
                            f"{download.playlist_item_index:02d} - {filename}"
                        )
                    download.filename = filename
                    download.save_path = str(save_dir / filename)
                    if info is not None:
                        download.detected_type = info.media_type
                        if info.size:
                            download.size_total = info.size
                    session.commit()

                stream_kwargs = dict(cookies=cookies, headers=headers, filename=filename)
                if format_selector:
                    stream_kwargs["format_selector"] = format_selector
                if merge_output_format:
                    stream_kwargs["merge_output_format"] = merge_output_format
                final_path = await asyncio.to_thread(
                    download_stream,
                    url,
                    save_dir,
                    quality,
                    self._make_stream_progress(download_id),
                    cancel_event,
                    **stream_kwargs,
                )

                self._finalize_stream(download_id, "completed", final_path)
            except DownloadCancelled:
                if not self._db_status_is(download_id, DownloadStatus.paused):
                    self._update_db_status(download_id, DownloadStatus.stopped)
            except StreamResolutionError as exc:
                # Browser-assist fallback: when yt-dlp hits a Cloudflare
                # bot-wall on an m3u8 URL, try fetching via the browser.
                if (
                    browser_assist
                    and (is_hls_or_dash_url(url) or is_bot_wall_error(str(exc)))
                ):
                    log.info(
                        "browser_assist_fallback_resolution",
                        id=download_id,
                        url=url,
                        error=str(exc)[:200],
                    )
                    final_path = await self._run_browser_stream(
                        download_id, url, save_dir, quality, headers,
                        cancel_event, existing_path,
                    )
                    if final_path is not None:
                        self._delete_parts(download_id)
                        self._finalize_stream(download_id, "completed", final_path)
                        return
                self._fail_stream(download_id, f"stream resolution failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - stream pipeline failure
                # Browser-assist fallback: when yt-dlp download fails with a
                # Cloudflare bot-wall on an m3u8 URL, try via the browser.
                if (
                    browser_assist
                    and (is_hls_or_dash_url(url) or is_bot_wall_error(str(exc)))
                ):
                    log.info(
                        "browser_assist_fallback_download",
                        id=download_id,
                        url=url,
                        error=str(exc)[:200],
                    )
                    final_path = await self._run_browser_stream(
                        download_id, url, save_dir, quality, headers,
                        cancel_event, existing_path,
                    )
                    if final_path is not None:
                        self._delete_parts(download_id)
                        self._finalize_stream(download_id, "completed", final_path)
                        return
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

    @staticmethod
    def _stream_headers(download) -> dict[str, str] | None:
        """Extra request headers (e.g. the player-page ``Referer``) captured by
        the extension and stored on the download record. The ``Cookie`` header
        is excluded here -- cookies travel through the yt-dlp cookiefile."""
        headers = dict(download.headers_json or {})
        headers.pop("cookie", None)
        return headers or None

    @staticmethod
    def _stream_request_context(download) -> RequestContext:
        """Rebuild the capture-time stream context (headers, cookies, page and
        frame URLs) persisted on a streaming download record.

        Consumed by the HLS pipeline (analyzer/download engine) started from a
        confirmed browser capture, where the extension is provably alive.
        """
        return RequestContext.from_download(download)

    def _prefer_player_host_sibling(
        self,
        download_id: int,
        url: str,
        headers: dict[str, str] | None,
    ) -> str | None:
        """Return a player-host sibling URL from the same page-scan batch as
        *url*, or ``None``.

        The ``fetch.*`` CDN master manifest 403s behind Cloudflare's bot-wall —
        only the player iframe (which holds the session cookie) can clear it.
        The same page scan also captured the player-host (``megaplay.*``)
        sibling, the host that already produced completed downloads (DB IDs
        159/164). Promote that sibling instead of retrying a host that is known
        to 403. Matching uses the player-page ``Referer`` captured on the
        capture rows, so only the same-episode sibling is promoted.
        """
        from urllib.parse import urlparse

        referer = None
        try:
            referer = (headers or {}).get("Referer")
        except Exception:  # pragma: no cover - defensive
            referer = None
        if not referer:
            return None
        with self.session_factory() as session:
            captures = PendingCaptureRepository(session).pending(limit=500)
        for capture in captures:
            candidate = (capture.url or "").strip()
            if not candidate or candidate == url:
                continue
            # The fetch.* CDN manifest is referred BY the player page; the
            # usable sibling is the player-host URL that matches that Referer.
            if candidate != referer:
                continue
            host = (urlparse(candidate).hostname or "").lower()
            if "megaplay" in host or "player" in host:
                return candidate
        return None

    async def _sniff_stream_page(
        self,
        url: str,
        headers: dict[str, str] | None,
    ) -> Any:
        """Fetch a streaming-site page in-process and return its embedded HLS/DASH
        manifest (:class:`SniffResult`), or ``None`` when the page carries none
        (e.g. a Cloudflare challenge page) or is unreachable.

        The manager supplies the transport (browser UA + ``follow_redirects``),
        keeping the sniff module network-free and deterministic to test.
        """
        from ...media.hls.sniff import PageSniffError, sniff_page_manifest

        request_headers: dict[str, str] = {
            "User-Agent": str(self.settings.get("network.user_agent", "MagnetoClip/0.1")),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        for key, value in (headers or {}).items():
            if key.lower() in {"referer", "user-agent", "cookie"}:
                request_headers[key] = value

        async def _fetch(page_url: str) -> tuple[str, str]:
            timeout = httpx.Timeout(
                float(self.settings.get("network.timeout_seconds", 15))
            )
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, headers=request_headers
            ) as client:
                response = await client.get(page_url)
                return str(response.status_code), response.text

        try:
            return await sniff_page_manifest(url, fetch=_fetch)
        except Exception as exc:  # noqa: BLE001 - sniffing is best-effort
            log.info("page_sniff_fetch_failed url=%s error=%s", url, str(exc)[:200])
            return None

    async def _run_browser_stream(
        self,
        download_id: int,
        url: str,
        save_dir: Path,
        quality: str,
        headers: dict[str, str] | None,
        cancel_event: threading.Event,
        existing_path: Path | None,
    ) -> Path | None:
        """Run a browser-assisted HLS download.

        Returns the finished file, or ``None`` (to fall back to yt-dlp) when
        the browser/extension is unavailable, the manifest could not be read,
        or a segment/key fetch failed mid-download. All pre-download and
        mid-download browser failures now fall back to yt-dlp so non-Cloudflare
        manifest hosts keep working without the browser.
        """
        from ...media import browser_streaming as browser_stream

        referer = headers.get("Referer") if headers else None
        frame_url = None
        cookies = None
        try:
            with self.session_factory() as session:
                row = DownloadRepository(session).get(download_id)
                if row is not None:
                    context = RequestContext.from_download(row)
                    frame_url = context.frame_url or context.referer
                    referer = context.referer or referer
                    cookies = self._stream_cookies(row)
        except Exception:
            log.debug("stream_context_lookup_failed", id=download_id, exc_info=True)
        basename = existing_path.name if existing_path is not None else None
        try:
            return await asyncio.to_thread(
                browser_stream.download_browser_hls,
                url,
                save_dir,
                quality,
                session_factory=self.session_factory,
                referer=referer,
                frame_url=frame_url,
                headers=headers,
                cookies=cookies,
                basename=basename,
                progress_cb=self._make_stream_progress(download_id),
                cancel_event=cancel_event,
                fetch_timeout=90.0,
            )
        except browser_stream.BrowserUnavailableError as exc:
            log.info(
                "browser_stream_unavailable_falling_back",
                id=download_id,
                error=str(exc),
            )
            return None
        except browser_stream.BrowserManifestError as exc:
            # The fetch.* CDN master manifest 403s (Cloudflare bot-wall that
            # only the player iframe can clear). The SAME page-scan batch also
            # captured the player-host (megaplay.*) sibling URL -- the host
            # that already produced completed downloads (DB IDs 159/164).
            # Promote that sibling instead of failing; the non-Cloudflare host
            # is the working one.
            sibling_url = self._prefer_player_host_sibling(download_id, url, headers)
            if sibling_url is not None:
                log.info(
                    "browser_stream_preferring_player_host_sibling_falling_back",
                    id=download_id,
                    url=sibling_url,
                    error=str(exc)[:200],
                )
                return await self._run_browser_stream(
                    download_id,
                    sibling_url,
                    save_dir,
                    quality,
                    headers,
                    cancel_event,
                    existing_path,
                )
            log.info(
                "browser_stream_manifest_failed_falling_back",
                id=download_id,
                error=str(exc),
            )
            return None
        except browser_stream.BrowserFetchError as exc:
            # Mid-download segment/key fetch failure — fall back to yt-dlp
            # rather than failing the download outright.
            log.info(
                "browser_stream_fetch_failed_falling_back",
                id=download_id,
                error=str(exc),
            )
            return None

    def _make_stream_progress(self, download_id: int):
        """Create a progress callback compatible with yt-dlp and the HLS engine.

        yt-dlp calls this hook with a single status dictionary, while the
        browser HLS engine reports ``(downloaded_bytes, total_bytes)``.
        Keeping one adapter here preserves the existing YouTube/yt-dlp
        behaviour while allowing HLS downloads to report progress safely.
        """

        def _hook(d, total=None) -> None:
            if isinstance(d, dict):
                status = d.get("status")
                if status != "downloading":
                    return
                downloaded = int(d.get("downloaded_bytes") or 0)
                total_bytes = int(
                    d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                )
                speed = d.get("speed")
                eta = d.get("eta")
            else:
                # HLS callback contract: (downloaded_bytes, total_bytes).
                downloaded = int(d or 0)
                total_bytes = int(total or 0)
                speed = None
                eta = None

            self.events.post(
                Events.PROGRESS_UPDATED,
                {
                    "id": download_id,
                    "downloaded": downloaded,
                    "total": total_bytes,
                },
            )
            if speed is not None:
                self.events.post(
                    Events.SPEED_UPDATED,
                    {"id": download_id, "speed": float(speed)},
                )
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
        self._sync_playlist_item(download, "failed", error)
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        self._post_notification("failed", snapshot)
        self.torrent_queue.admit_and_advance()

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
        self._sync_playlist_item(download, "completed")
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        self._post_notification("completed", snapshot)
        self.torrent_queue.admit_and_advance()
        self._inspect_media_async(download_id)

    # ----- torrent execution -----

    def _start_torrent(self, download_id: int) -> bool:
        """Start a torrent download via the TorrentDownloadHandler."""
        if self._torrent_client is None:
            self._init_torrent_client()
        if self._torrent_client is None:
            log.warning("torrent_client_not_available", download_id=download_id)
            self._fail_torrent(
                download_id,
                "libtorrent is not installed. Install it with: pip install libtorrent",
            )
            return False

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

        task = asyncio.create_task(self._run_torrent(download_id))
        self._tasks[download_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(download_id, None))
        return True

    async def _run_torrent(self, download_id: int) -> None:
        """Download a torrent via the TorrentDownloadHandler, mirroring events
        into the normal progress/state pipeline."""
        async with self.semaphore:
            try:
                with self.session_factory() as session:
                    download = DownloadRepository(session).get(download_id)
                    if download is None:
                        return
                    spec = self._build_torrent_spec(download)
                    save_dir = Path(spec.save_dir)
                    save_dir.mkdir(parents=True, exist_ok=True)

                self.events.post(
                    Events.DOWNLOAD_STATE_CHANGED,
                    {"id": download_id, "state": "connecting"},
                )

                handler = TorrentDownloadHandler(
                    client=self._torrent_client,
                    bus=self.events,
                    spec=spec,
                )
                self._torrent_handlers[download_id] = handler
                result = await handler.run()
                self._finalize_torrent(download_id, result, handler)
            except Exception as exc:
                log.warning(
                    "torrent_download_failed",
                    download_id=download_id,
                    error=str(exc),
                )
                self._fail_torrent(download_id, str(exc))
            finally:
                self._torrent_handlers.pop(download_id, None)

    def _build_torrent_spec(self, download: Download) -> TorrentSpec:
        """Build a TorrentSpec from a Download record."""
        url = download.url
        save_dir = Path(download.save_path).parent if download.save_path else Path(
            self.settings.get("torrent.default_save_dir", "")
            or self.settings.get("downloads.default_directory", str(Path.home() / "Downloads"))
        )
        save_dir.mkdir(parents=True, exist_ok=True)

        opts = self._pending_torrent_opts.pop(download.id, {})
        sequential = opts.get("sequential", bool(download.torrent_sequential))
        if sequential is None:
            sequential = bool(self.settings.get("torrent.default_sequential", False))
        seed = opts.get("seed_mode", bool(self.settings.get("torrent.auto_seed", False)))
        connections = int(self.settings.get("torrent.connections_per_torrent", 0) or 0)

        common: dict[str, Any] = {
            "download_id": download.id,
            "save_dir": save_dir,
            "sequential": sequential,
            "seed_mode": seed,
            "connections_max": connections,
            "resume_sidecar": (
                str(sidecar_path_for(download.save_path)) if download.save_path else ""
            ),
        }

        if is_magnet_link(url):
            return TorrentSpec(
                filename=download.filename or "torrent_download",
                magnet_uri=url,
                **common,
            )

        local_path = Path(url) if url else Path()
        if is_torrent_file_url(url) and not local_path.is_file():
            torrent_path = self._download_torrent_file(
                url, save_dir, target=self._torrent_meta_target(download)
            )
            return TorrentSpec(
                filename=download.filename or Path(url).name,
                torrent_file_path=torrent_path,
                **common,
            )

        if is_torrent_file_url(url) and local_path.is_file():
            return TorrentSpec(
                filename=download.filename or local_path.name,
                torrent_file_path=str(local_path),
                **common,
            )

        lower = url.strip().lower()
        if lower.startswith(("http://", "https://")):
            torrent_path = self._download_torrent_file(
                url, save_dir, target=self._torrent_meta_target(download)
            )
            return TorrentSpec(
                filename=download.filename or "torrent_download",
                torrent_file_path=torrent_path,
                **common,
            )

        return TorrentSpec(
            filename=download.filename or "torrent_download",
            torrent_file_path=None,
            **common,
        )

    @staticmethod
    def _torrent_meta_target(download) -> Path | None:
        """Return the on-disk path where the .torrent metadata should live.

        The download record's ``save_path`` is the promised .torrent location,
        so the metadata file is written there to keep the record and the
        filesystem in sync (and allow the completed download to be previewed).
        """
        save_path = getattr(download, "save_path", None)
        if save_path and str(save_path).lower().endswith(".torrent"):
            return Path(save_path)
        return None

    def _download_torrent_file(
        self, url: str, dest_dir: Path, *, target: Path | None = None
    ) -> Path:
        """Download a .torrent file from *url* and return its on-disk path.

        When *target* is given (the download's ``save_path``), the metadata is
        written there so completed torrent downloads can be previewed from the
        downloads page; otherwise a seeded temp name is used.
        """
        import uuid

        dest_dir.mkdir(parents=True, exist_ok=True)
        torrent_path = target or dest_dir / f".torrent_{uuid.uuid4().hex[:12]}.tmp"
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(url)
            resp.raise_for_status()
            torrent_path.write_bytes(resp.content)
        return torrent_path

    def _fail_torrent(self, download_id: int, error: str) -> None:
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
        self.torrent_queue.admit_and_advance()

    def _finalize_torrent(
        self, download_id: int, result: str, handler: TorrentDownloadHandler
    ) -> None:
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            status = handler._state if result == "completed" else result
            if result == "completed":
                download.status = DownloadStatus.completed
                download.completed_at = _now()
                download.torrent_seeding = handler._state == "seeding"
            elif result == "failed":
                download.status = DownloadStatus.failed
                download.error = handler.error or "torrent download failed"
            elif result == "stopped":
                download.status = DownloadStatus.stopped
            session.commit()
            download = repo.get(download_id)
            snapshot = self.snapshot_item(download) if download else {"id": download_id}
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        if result == "completed":
            self._post_notification("completed", snapshot)
        elif result == "failed":
            self._post_notification("failed", snapshot)
        self.torrent_queue.admit_and_advance()

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
                self._reconcile_final_path(download, task)
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
        self.torrent_queue.admit_and_advance()
        self._post_notification(result, snapshot)
        if result == "completed":
            self._index_dedup_async(download_id)

    def _index_dedup_async(self, download_id: int) -> None:
        """Index a completed download's file in the dedup index (off-thread)."""
        dedup = getattr(self.context, "dedup", None)
        if dedup is None:
            return
        try:
            asyncio.create_task(self._index_dedup(download_id))
        except RuntimeError:
            pass

    async def _index_dedup(self, download_id: int) -> None:
        dedup = getattr(self.context, "dedup", None)
        if dedup is None:
            return
        with self.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
            if download is None or not download.save_path:
                return
            path = download.save_path
            filename = download.filename or Path(path).name
            category_id = download.category_id
        duplicate = await asyncio.to_thread(
            dedup.index_file_and_check, path, filename, category_id
        )
        if duplicate is not None and duplicate.is_duplicate:
            self.events.post(
                Events.DUPLICATE_DETECTED,
                {
                    "filename": filename,
                    "url": "",
                    "save_path": path,
                    "existing_path": duplicate.existing_path,
                    "existing_filename": duplicate.existing_filename,
                },
            )

    def _index_single(self, path: str, filename: str, category_id, algo: str) -> bool:
        raise NotImplementedError  # replaced by DedupManager.index_file_and_check

    @staticmethod
    def _reconcile_final_path(download, task) -> None:
        """Persist the true on-disk filename if the engine adjusted it.

        The engine may append an extension derived from the response
        Content-Type when the URL gave no filename (e.g. Google's
        extensionless ``encrypted-tbn0.gstatic.com`` image thumbnails). Keep the
        download record's filename/save_path in sync with where the file was
        actually written so the UI list and the filesystem agree.
        """
        final = getattr(task.spec, "final_path", None)
        if final is None:
            return
        final_path = Path(final)
        if str(final_path) == (download.save_path or "") and final_path.name == (
            download.filename or ""
        ):
            return
        download.filename = final_path.name
        download.save_path = str(final_path)

    def _post_notification(self, result: str | None, snapshot: dict) -> None:
        title = snapshot.get("filename") or f"Download #{snapshot.get('id')}"
        if result == "started":
            kind = "started"
            body = "Download started"
        elif result == "completed":
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
                "save_path": snapshot.get("save_path"),
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
            locator = FFmpegLocator(settings=self.settings)
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
            # Actual resolution the platform served (the requested selector may
            # have fallen back to a lower render via the ``[height<=N]`` rule):
            height = metadata.get("height") if metadata else None
            if isinstance(height, int) and height:
                download.actual_height = height
                download.actual_width = metadata.get("width")
                download.actual_fps = metadata.get("fps")
            session.commit()
            snapshot = self.snapshot_item(download)
            if download.playlist_id and download.playlist_item_index is not None:
                self._sync_playlist_actual_quality(download, height)
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
            first_start = download.started_at is None and "started_at" in fields
            if download.started_at is not None and "started_at" in fields:
                del fields["started_at"]
            for key, value in fields.items():
                setattr(download, key, value)
            download.status = status
            session.commit()
            snapshot = self.snapshot_item(download)
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)
        if status is DownloadStatus.downloading and first_start:
            self._post_notification("started", snapshot)

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
            reported = int(payload.get("downloaded") or 0)
            # Torrent progress is strictly monotonic for one download run: a dip
            # back to ~0 happens while libtorrent re-checks files after a
            # resume/re-add and would otherwise show the download as restarted.
            # Keep the highest value seen so the DB/UI never regress the bar;
            # explicit restarts zero the counter in restart() before starting.
            if download.size_downloaded and reported < download.size_downloaded:
                if download.detected_type == "torrent" or is_torrent_url(
                    download.url or ""
                ):
                    reported = download.size_downloaded
            download.size_downloaded = reported
            if payload.get("total"):
                download.size_total = int(payload["total"])
            if payload.get("speed") is not None:
                speed = float(payload["speed"])
                download.speed_avg = speed
                download.speed_peak = max(download.speed_peak or 0.0, speed)
            for key, attr in (
                ("num_peers", "torrent_num_peers"),
                ("num_seeds", "torrent_num_seeds"),
                ("num_pieces", "torrent_num_pieces"),
                ("piece_size", "torrent_piece_size"),
            ):
                if key in payload and payload[key] is not None:
                    setattr(download, attr, int(payload[key]))
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
            limit = int(payload.get("max") or 0)
            if limit > 0:
                download.connections_max = limit
            session.commit()

    def _on_torrent_name_resolved(self, payload: Any) -> None:
        """Persist the real torrent title once libtorrent learns it.

        Magnets without a ``dn=`` display name are stored with a 'magnet_'
        placeholder at add-time. When the actual torrent name arrives from the
        swarm we adopt it as the filename (which the UI Name column reflects)
        and refresh the torrent's info hash if we didn't have it.
        """
        if not isinstance(payload, dict):
            return
        download_id = payload.get("id")
        filename = (payload.get("filename") or "").strip()
        if not download_id or not filename:
            return
        name = sanitize_filename(filename)
        if not name:
            return
        info_hash = (payload.get("info_hash") or "").strip()
        with self.session_factory() as session:
            repo = DownloadRepository(session)
            download = repo.get(download_id)
            if download is None:
                return
            if download.filename and not self._looks_like_placeholder(
                download.filename
            ):
                # A real name was already set (e.g. from a .torrent file or a
                # magnet dn=); keep it rather than overwriting.
                return
            download.filename = name
            if info_hash and (
                not download.torrent_info_hash
                or download.torrent_info_hash == ""
            ):
                download.torrent_info_hash = info_hash
            session.commit()
            snapshot = self.snapshot_item(download)
        self.events.post(Events.DOWNLOAD_UPDATED, snapshot)

    @staticmethod
    def _looks_like_placeholder(filename: str) -> bool:
        """True if *filename* looks like the URL-derived 'magnet' placeholder."""
        lowered = (filename or "").strip().lower()
        return (
            lowered in ("magnet", "magnet_", "download")
            or lowered.startswith("magnet:")
            or lowered.startswith("magnet_")
        )

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
        self._apply_torrent_limits()
        self.torrent_queue.admit_and_advance()

    # ----- helpers -----

    def _apply_torrent_limits(self) -> None:
        """Push torrent connection/rate-limit settings to the live session."""
        if self._torrent_client is None:
            return
        from ...torrent.client import ClientConfig as TConfig

        down_kb = int(self.settings.get("torrent.max_download_speed", 0) or 0)
        up_kb = int(self.settings.get("torrent.max_upload_speed", 0) or 0)
        self._torrent_client.apply_session_limits(
            max_connections=int(self.settings.get("torrent.max_connections", 500)),
            max_uploads=int(self.settings.get("torrent.max_uploads", 4)),
            download_bps=down_kb * 1024,
            upload_bps=up_kb * 1024,
        )
        # Throughput tuning (cache, threads, request queue, TCP preference) can
        # be changed without restarting the session.
        self._torrent_client.apply_tuning(TConfig.from_settings(self.settings))

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
            in_place=bool(self.settings.get("downloads.inplace_downloads", True)),
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

    @staticmethod
    def _validate_url(url: str) -> None:
        if is_torrent_url(url):
            return
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
            "torrent_info_hash": download.torrent_info_hash,
            "torrent_num_peers": download.torrent_num_peers,
            "torrent_num_seeds": download.torrent_num_seeds,
            "torrent_num_pieces": download.torrent_num_pieces,
            "torrent_piece_size": download.torrent_piece_size,
            "torrent_sequential": download.torrent_sequential,
            "torrent_seeding": download.torrent_seeding,
            "youtube_quality": download.youtube_quality,
            "youtube_format": download.youtube_format,
            "youtube_format_label": download.youtube_format_label,
            "youtube_requested_quality": download.youtube_requested_quality,
            "actual_height": download.actual_height,
            "actual_width": download.actual_width,
            "actual_fps": download.actual_fps,
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
