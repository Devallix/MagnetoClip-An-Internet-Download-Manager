"""Download queues: ordered lists with concurrency limits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from ...app.context import AppContext
from ...database.models import Download, DownloadStatus, Queue
from ...database.repositories import QueueRepository
from ...services.logging import get_logger
from ..events.bus import Events

if TYPE_CHECKING:
    from ..downloads.manager import DownloadManager

log = get_logger(__name__)

PENDING_STATUSES = (DownloadStatus.queued, DownloadStatus.scheduled)


class QueueManager:
    """Owns the download queues and advances their next items."""

    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.session_factory = context.session_factory
        self._manager: DownloadManager | None = None

    @property
    def manager(self) -> DownloadManager:
        if self._manager is None:
            raise RuntimeError("DownloadManager not attached to QueueManager")
        return self._manager

    def attach(self, manager: DownloadManager) -> None:
        self._manager = manager

    # ----- CRUD -----

    def list(self) -> list[Queue]:
        with self.session_factory() as session:
            return QueueRepository(session).list()

    def get(self, queue_id: int) -> Queue | None:
        with self.session_factory() as session:
            return QueueRepository(session).get(queue_id)

    def update(
        self,
        queue_id: int,
        *,
        name: str | None = None,
        max_concurrent: int | None = None,
    ) -> Queue:
        with self.session_factory() as session:
            queue = QueueRepository(session).update(
                queue_id, name=name, max_concurrent=max_concurrent
            )
        self.context.events.post(Events.QUEUE_UPDATED, {"id": queue_id})
        return queue

    def add(self, name: str, *, max_concurrent: int = 3) -> Queue:
        with self.session_factory() as session:
            repo = QueueRepository(session)
            queue = repo.add(name, max_concurrent=max_concurrent)
        self.context.events.post(Events.QUEUE_ADDED, {"id": queue.id, "name": queue.name})
        return queue

    def remove(self, queue_id: int) -> None:
        with self.session_factory() as session:
            repo = QueueRepository(session)
            queue = repo.get(queue_id)
            if queue is None:
                raise KeyError(queue_id)
            for item in repo.items(queue_id):
                if item.download is not None:
                    item.download.queue_id = None
            repo.remove(queue)
        self.context.events.post(Events.QUEUE_REMOVED, {"id": queue_id})

    # ----- membership -----

    def add_download(
        self, queue_id: int, download_id: int, *, auto_start: bool = True
    ) -> None:
        with self.session_factory() as session:
            repo = QueueRepository(session)
            repo.add_item(queue_id, download_id)
        self.context.events.post(Events.QUEUE_UPDATED, {"id": queue_id})
        if auto_start:
            self.advance(queue_id)

    def remove_download(self, queue_id: int, download_id: int) -> None:
        with self.session_factory() as session:
            QueueRepository(session).remove_item(queue_id, download_id)
        self.context.events.post(Events.QUEUE_UPDATED, {"id": queue_id})

    def items(self, queue_id: int) -> list[object]:
        with self.session_factory() as session:
            return QueueRepository(session).items(queue_id)

    def reorder(self, queue_id: int, ordered_ids: list[int]) -> None:
        with self.session_factory() as session:
            QueueRepository(session).reorder(queue_id, ordered_ids)
        self.context.events.post(Events.QUEUE_UPDATED, {"id": queue_id})

    # ----- advancement -----

    def pending_downloads(self, queue_id: int) -> list[Download]:
        """Pending items, highest priority first (positions tie-break)."""
        with self.session_factory() as session:
            repo = QueueRepository(session)
            downloads = [item.download for item in repo.items(queue_id) if item.download]
        pending = [dl for dl in downloads if dl.status in PENDING_STATUSES]
        return sorted(pending, key=lambda dl: (-(dl.priority or 0), pending.index(dl)))

    def advance(self, queue_id: int) -> None:
        """Start the next queued item once capacity frees up."""
        with self.session_factory() as session:
            queue = QueueRepository(session).get(queue_id)
            if queue is None:
                return
            max_concurrent = queue.max_concurrent
        active = self.manager.count_active_in_queue(queue_id)
        for download in self.pending_downloads(queue_id):
            if active >= max_concurrent:
                break
            self.manager.start(download.id, queue_advance=True)
            active += 1
