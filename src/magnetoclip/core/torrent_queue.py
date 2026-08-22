"""Global torrent admission queue driven by settings.

MagnetoClip queues torrent downloads automatically using two limits:

- ``torrent.max_active_torrents`` caps how many *unfinished* torrents may
  hold a queue slot (status ``queued``). Additional additions wait with
  status ``scheduled`` (shown as "Waiting") and are promoted oldest-first
  as slots free up.
- ``torrent.max_active_downloads`` caps how many admitted torrents may
  transfer at the same time. The rest stay ``queued`` until capacity frees.

HTTP/streaming downloads are not affected by this queue. Completed torrents
that are seeding do not occupy slots.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..database.models import Download, DownloadStatus
from ..services.logging import get_logger
from .events.bus import Events

log = get_logger(__name__)

TRANSFER_STATUSES = (
    DownloadStatus.connecting,
    DownloadStatus.downloading,
    DownloadStatus.retrying,
    DownloadStatus.verifying,
)
UNFINISHED_STATUSES = TRANSFER_STATUSES + (
    DownloadStatus.queued,
    DownloadStatus.scheduled,
    DownloadStatus.paused,
)

# Statuses that never occupy a transfer slot.
SKIP_SLOT_STATUSES = (
    DownloadStatus.paused,
    DownloadStatus.completed,
    DownloadStatus.failed,
    DownloadStatus.verification_failed,
    DownloadStatus.stopped,
)


class TorrentQueue:
    """Admission control for torrent downloads."""

    def __init__(self, manager) -> None:
        self.manager = manager

    # ----- settings -----

    @property
    def max_active_torrents(self) -> int:
        return max(
            1, int(self.manager.settings.get("torrent.max_active_torrents", 5))
        )

    @property
    def max_active_downloads(self) -> int:
        return max(
            1, int(self.manager.settings.get("torrent.max_active_downloads", 3))
        )

    def slots_full(self) -> bool:
        """True when every download slot is taken by an active torrent."""
        return self.count_transfers() >= self.max_active_downloads

    # ----- queries -----

    def _unfinished(self) -> list[dict[str, Any]]:
        """Unfinished, non-seeding torrents ordered by queue position."""
        with self.manager.session_factory() as session:
            rows = session.execute(
                select(Download.id, Download.status)
                .where(
                    Download.detected_type == "torrent",
                    Download.torrent_seeding.is_(False),
                    Download.status.in_(UNFINISHED_STATUSES),
                )
                .order_by(
                    Download.priority.desc(),
                    Download.created_at.asc(),
                    Download.id.asc(),
                )
            ).all()
        return [{"id": row[0], "status": row[1]} for row in rows]

    def count_unfinished(self) -> int:
        return len(self._unfinished())

    def count_admitted(self) -> int:
        return sum(
            1
            for item in self._unfinished()
            if item["status"] != DownloadStatus.scheduled
        )

    def count_transfers(self) -> int:
        """Torrents occupying a transfer slot right now.

        Active statuses always count. Starting torrents whose machinery is
        alive but has not reported an active status yet count as well.
        Paused torrents park their libtorrent handler intentionally and do
        NOT hold a slot — even while waiting in the queue again after a
        resume under load — so pausing frees capacity for others.
        """
        handlers_map = self.manager._torrent_handlers
        tasks = set(self.manager._tasks)
        with self.manager.session_factory() as session:
            rows = session.execute(
                select(Download.id, Download.status).where(
                    Download.detected_type == "torrent",
                    Download.torrent_seeding.is_(False),
                )
            ).all()
        total = 0
        for download_id, status in rows:
            if status in SKIP_SLOT_STATUSES:
                continue
            if status in TRANSFER_STATUSES:
                total += 1
                continue
            if download_id in set(handlers_map) or download_id in tasks:
                handler = handlers_map.get(download_id)
                parked = False
                if handler is not None and hasattr(handler, "is_paused"):
                    try:
                        parked = bool(handler.is_paused())
                    except Exception:  # noqa: BLE001 - defensive only
                        parked = False
                if not parked:
                    # Starting up: machinery exists but no active status yet.
                    total += 1
        return total

    def queued_ids(self) -> list[int]:
        return [
            item["id"]
            for item in self._unfinished()
            if item["status"] == DownloadStatus.queued
        ]

    # ----- reconciliation -----

    def reconcile(self) -> None:
        """Align queued/scheduled statuses with ``max_active_torrents``."""
        items = self._unfinished()
        limit = self.max_active_torrents
        updates: list[tuple[int, DownloadStatus]] = []
        for index, item in enumerate(items):
            want = index < limit
            status = item["status"]
            if want and status == DownloadStatus.scheduled:
                updates.append((item["id"], DownloadStatus.queued))
            elif not want and status == DownloadStatus.queued:
                updates.append((item["id"], DownloadStatus.scheduled))
        if not updates:
            return
        events = self.manager.events
        from ..database.repositories import DownloadRepository

        for download_id, status in updates:
            with self.manager.session_factory() as session:
                download = DownloadRepository(session).update_status(
                    download_id, status
                )
                snapshot = (
                    self.manager.snapshot_item(download) if download else None
                )
            if snapshot:
                events.post(Events.DOWNLOAD_UPDATED, snapshot)
                log.info("torrent_queue_status", id=download_id, status=status.value)

    def advance(self) -> None:
        """Reconcile then start queued torrents while capacity remains."""
        self.reconcile()
        limit = self.max_active_downloads
        active = self.count_transfers()
        if active >= limit:
            return
        for download_id in self.queued_ids():
            if active >= limit:
                break
            try:
                if self.manager.start(download_id, queue_advance=True):
                    active += 1
            except Exception:  # noqa: BLE001 - one bad item must not stall the queue
                log.exception("torrent_queue_start_failed", id=download_id)

    def admit_and_advance(self) -> None:
        self.advance()

    def resume(self, download_id: int) -> None:
        """Resume a paused torrent while respecting the download limit.

        With a free slot the torrent resumes immediately (its parked
        libtorrent handler is reused when one exists). Otherwise it goes
        back to the waiting queue and starts automatically as soon as
        capacity frees up.
        """
        from ..database.repositories import DownloadRepository

        with self.manager.session_factory() as session:
            download = DownloadRepository(session).get(download_id)
        if download is None or download.status != DownloadStatus.paused:
            return
        if self.slots_full():
            self.manager._update_db_status(download_id, DownloadStatus.queued)
            log.info("torrent_resume_requeued", id=download_id)
            self.advance()
            return
        self.manager.start(download_id, queue_advance=True)

    async def shutdown(self) -> None:  # parity with other context services
        return None
