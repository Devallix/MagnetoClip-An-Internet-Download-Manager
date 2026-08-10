"""Data-access layer for all persistent entities."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .models import (
    BrowserDetection,
    Category,
    Download,
    DownloadStatus,
    PendingCapture,
    ProxyProfile,
    Queue,
    QueueItem,
    Schedule,
    Setting,
)

_UNSET = object()


class DownloadRepository:
    """Data-access operations for downloads."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        url: str,
        *,
        filename: str | None = None,
        save_path: str | None = None,
        category_id: int | None = None,
        queue_id: int | None = None,
        priority: int = 0,
        connections_max: int = 8,
        headers: dict[str, Any] | None = None,
        hash_algo: str | None = None,
        hash_expected: str | None = None,
        proxy_profile_id: int | None = None,
        auth_ref: str | None = None,
    ) -> Download:
        download = Download(
            url=url,
            filename=filename,
            save_path=save_path,
            category_id=category_id,
            queue_id=queue_id,
            priority=priority,
            connections_max=connections_max,
            headers_json=headers,
            hash_algo=hash_algo,
            hash_expected=hash_expected,
            proxy_profile_id=proxy_profile_id,
            auth_ref=auth_ref,
        )
        self.session.add(download)
        self.session.commit()
        self.session.refresh(download)
        return download

    def get(self, download_id: int) -> Optional[Download]:
        return self.session.get(Download, download_id)

    def list(
        self,
        *,
        status: DownloadStatus | None = None,
        category_id: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Download]:
        statement = select(Download).order_by(Download.created_at.desc())
        if status is not None:
            statement = statement.where(Download.status == status)
        if category_id is not None:
            statement = statement.where(Download.category_id == category_id)
        return list(
            self.session.scalars(statement.limit(limit).offset(offset)).all()
        )

    def update_status(
        self,
        download_id: int,
        status: DownloadStatus,
        **fields: Any,
    ) -> Optional[Download]:
        download = self.get(download_id)
        if download is None:
            return None
        download.status = status
        for key, value in fields.items():
            if hasattr(download, key):
                setattr(download, key, value)
        self.session.commit()
        return download

    def remove(self, download: Download) -> None:
        self.session.delete(download)
        self.session.commit()


class CategoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[Category]:
        return list(self.session.scalars(select(Category).order_by(Category.name)).all())

    def get(self, category_id: int) -> Optional[Category]:
        return self.session.get(Category, category_id)

    def get_by_name(self, name: str) -> Optional[Category]:
        return self.session.scalar(
            select(Category).where(Category.name == name)
        )

    def add(
        self,
        name: str,
        *,
        folder: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        rules: dict[str, Any] | None = None,
    ) -> Category:
        category = Category(
            name=name, folder=folder, icon=icon, color=color, rules_json=rules or {}
        )
        self.session.add(category)
        self.session.commit()
        self.session.refresh(category)
        return category

    def remove(self, category: Category) -> None:
        self.session.delete(category)
        self.session.commit()


class QueueRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[Queue]:
        return list(self.session.scalars(select(Queue).order_by(Queue.name)).all())

    def get(self, queue_id: int) -> Optional[Queue]:
        return self.session.get(Queue, queue_id)

    def add(self, name: str, *, max_concurrent: int = 3) -> Queue:
        queue = Queue(name=name, max_concurrent=max_concurrent)
        self.session.add(queue)
        self.session.commit()
        self.session.refresh(queue)
        return queue

    def update(
        self,
        queue_id: int,
        *,
        name: str | None = None,
        max_concurrent: int | None = None,
    ) -> Queue:
        queue = self.session.get(Queue, queue_id)
        if queue is None:
            raise KeyError(queue_id)
        if name is not None:
            queue.name = name
        if max_concurrent is not None:
            queue.max_concurrent = max_concurrent
        self.session.commit()
        self.session.refresh(queue)
        return queue

    def remove(self, queue: Queue) -> None:
        self.session.delete(queue)
        self.session.commit()

    def items(self, queue_id: int) -> list[QueueItem]:
        return list(
            self.session.scalars(
                select(QueueItem)
                .where(QueueItem.queue_id == queue_id)
                .options(selectinload(QueueItem.download))
                .order_by(QueueItem.position, QueueItem.id)
            ).all()
        )

    def add_item(self, queue_id: int, download_id: int) -> QueueItem:
        position = len(self.items(queue_id))
        item = QueueItem(queue_id=queue_id, download_id=download_id, position=position)
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def remove_item(self, queue_id: int, download_id: int) -> None:
        item = self.session.scalar(
            select(QueueItem).where(
                QueueItem.queue_id == queue_id,
                QueueItem.download_id == download_id,
            )
        )
        if item is not None:
            self.session.delete(item)
            self.session.commit()

    def reorder(self, queue_id: int, ordered_ids: list[int]) -> None:
        items = self.items(queue_id)
        by_id = {item.download_id: item for item in items}
        for position, download_id in enumerate(ordered_ids):
            item = by_id.get(download_id)
            if item is not None:
                item.position = position
        self.session.commit()

    def queue_ids_for_download(self, download_id: int) -> list[int]:
        return list(
            self.session.scalars(
                select(QueueItem.queue_id).where(
                    QueueItem.download_id == download_id
                )
            ).all()
        )


class ScheduleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[Schedule]:
        return list(self.session.scalars(select(Schedule).order_by(Schedule.id)).all())

    def get(self, schedule_id: int) -> Optional[Schedule]:
        return self.session.get(Schedule, schedule_id)

    def add(
        self,
        name: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        days_mask: int = 0b1111111,
        speed_day: float | None = None,
        speed_night: float | None = None,
        enabled: bool = False,
    ) -> Schedule:
        schedule = Schedule(
            name=name,
            start_time=start_time,
            end_time=end_time,
            days_mask=days_mask,
            speed_day=speed_day,
            speed_night=speed_night,
            enabled=enabled,
        )
        self.session.add(schedule)
        self.session.commit()
        self.session.refresh(schedule)
        return schedule

    def remove(self, schedule: Schedule) -> None:
        self.session.delete(schedule)
        self.session.commit()

    def update(
        self,
        schedule_id: int,
        *,
        name: Any = _UNSET,
        start_time: Any = _UNSET,
        end_time: Any = _UNSET,
        days_mask: Any = _UNSET,
        speed_day: Any = _UNSET,
        speed_night: Any = _UNSET,
        enabled: Any = _UNSET,
    ) -> Schedule:
        schedule = self.session.get(Schedule, schedule_id)
        if schedule is None:
            raise KeyError(schedule_id)
        if name is not _UNSET:
            schedule.name = name
        if start_time is not _UNSET:
            schedule.start_time = start_time
        if end_time is not _UNSET:
            schedule.end_time = end_time
        if days_mask is not _UNSET:
            schedule.days_mask = days_mask
        if speed_day is not _UNSET:
            schedule.speed_day = speed_day
        if speed_night is not _UNSET:
            schedule.speed_night = speed_night
        if enabled is not _UNSET:
            schedule.enabled = enabled
        self.session.commit()
        self.session.refresh(schedule)
        return schedule


class ProxyRepository:
    """Data-access operations for proxy profiles."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[ProxyProfile]:
        return list(
            self.session.scalars(select(ProxyProfile).order_by(ProxyProfile.name)).all()
        )

    def get(self, profile_id: int) -> Optional[ProxyProfile]:
        return self.session.get(ProxyProfile, profile_id)

    def get_by_name(self, name: str) -> Optional[ProxyProfile]:
        return self.session.scalar(
            select(ProxyProfile).where(ProxyProfile.name == name)
        )

    def add(
        self,
        name: str,
        *,
        proxy_type: str = "direct",
        host: str | None = None,
        port: int | None = None,
        username_ref: str | None = None,
    ) -> ProxyProfile:
        profile = ProxyProfile(
            name=name,
            type=proxy_type,
            host=host,
            port=port,
            username_ref=username_ref,
        )
        self.session.add(profile)
        self.session.commit()
        self.session.refresh(profile)
        return profile

    def remove(self, profile: ProxyProfile) -> None:
        self.session.delete(profile)
        self.session.commit()


class PendingCaptureRepository:
    """Data-access operations for browser captures awaiting confirmation."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        url: str,
        *,
        filename: str | None = None,
        referrer: str | None = None,
        source: str | None = None,
        detected_type: str | None = None,
    ) -> PendingCapture:
        capture = PendingCapture(
            url=url,
            filename=filename,
            referrer=referrer,
            source=source,
            detected_type=detected_type,
            status="pending",
        )
        self.session.add(capture)
        self.session.commit()
        self.session.refresh(capture)
        return capture

    def pending(self, limit: int = 100) -> list[PendingCapture]:
        return list(
            self.session.scalars(
                select(PendingCapture)
                .where(PendingCapture.status == "pending")
                .order_by(PendingCapture.created_at.asc())
                .limit(limit)
            ).all()
        )

    def get(self, capture_id: int) -> Optional[PendingCapture]:
        return self.session.get(PendingCapture, capture_id)

    def resolve(
        self,
        capture_id: int,
        status: str,
        *,
        download_id: int | None = None,
    ) -> Optional[PendingCapture]:
        capture = self.session.get(PendingCapture, capture_id)
        if capture is None:
            return None
        capture.status = status
        if download_id is not None:
            capture.download_id = download_id
        self.session.commit()
        return capture

    def expire_stale(self, max_age_seconds: int = 600) -> int:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        stale = list(
            self.session.scalars(
                select(PendingCapture).where(
                    PendingCapture.status == "pending",
                    PendingCapture.created_at < cutoff,
                )
            ).all()
        )
        for capture in stale:
            capture.status = "expired"
        if stale:
            self.session.commit()
        return len(stale)


class BrowserDetectionRepository:
    """Data-access operations for page-scan detections."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        page_url: str,
        *,
        count: int,
        files: list[dict[str, Any]] | None = None,
    ) -> BrowserDetection:
        detection = BrowserDetection(
            page_url=page_url, count=count, files_json=files or []
        )
        self.session.add(detection)
        self.session.commit()
        self.session.refresh(detection)
        return detection

    def unnotified(self, limit: int = 50) -> list[BrowserDetection]:
        return list(
            self.session.scalars(
                select(BrowserDetection)
                .where(BrowserDetection.notified.is_(False))
                .order_by(BrowserDetection.created_at.asc())
                .limit(limit)
            ).all()
        )

    def mark_notified(self, detection_id: int) -> None:
        detection = self.session.get(BrowserDetection, detection_id)
        if detection is not None:
            detection.notified = True
            self.session.commit()


class SettingsStore:
    """Persists the flat settings model in the ``settings`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def load_all(self) -> dict[str, Any]:
        with self.session_factory() as session:
            rows = session.scalars(select(Setting)).all()
            return {row.key: row.value_json for row in rows}

    def save(self, key: str, value: Any) -> None:
        with self.session_factory() as session:
            self._upsert(session, key, value)
            session.commit()

    def save_many(self, mapping: dict[str, Any]) -> None:
        with self.session_factory() as session:
            for key, value in mapping.items():
                self._upsert(session, key, value)
            session.commit()

    @staticmethod
    def _upsert(session: Session, key: str, value: Any) -> None:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value_json=value))
        else:
            row.value_json = value
