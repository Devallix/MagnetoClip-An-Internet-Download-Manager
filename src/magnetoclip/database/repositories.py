"""Data-access layer for all persistent entities."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    BrowserDetection,
    BrowserRequest,
    Category,
    Download,
    DownloadStatus,
    FileHash,
    PendingCapture,
    ProxyProfile,
    Setting,
    SpeedTest,
)


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
        cookies: dict[str, str] | None = None,
        data_base64: str | None = None,
        status: str = "pending",
    ) -> PendingCapture:
        capture = PendingCapture(
            url=url,
            filename=filename,
            referrer=referrer,
            source=source,
            detected_type=detected_type,
            cookies_json=cookies or None,
            data_base64=data_base64,
            status=status,
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

    def resolve_all(self, status: str, limit: int = 100) -> int:
        pending = self.pending(limit=limit)
        for capture in pending:
            capture.status = status
        if pending:
            self.session.commit()
        return len(pending)

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
        notified: bool = False,
    ) -> BrowserDetection:
        detection = BrowserDetection(
            page_url=page_url,
            count=count,
            files_json=files or [],
            notified=notified,
        )
        self.session.add(detection)
        self.session.commit()
        self.session.refresh(detection)
        return detection

    def known_file_urls(self, limit: int = 2000) -> set[str]:
        """URLs recorded by earlier detections.

        Used to suppress repeat tray notifications when a rescan of the same
        page finds nothing new.
        """
        rows = self.session.scalars(
            select(BrowserDetection.files_json)
            .order_by(BrowserDetection.created_at.desc())
            .limit(limit)
        ).all()
        urls: set[str] = set()
        for files in rows:
            for file in files or []:
                url = str((file or {}).get("url") or "").strip()
                if url:
                    urls.add(url)
        return urls

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

    def list_detections(self, limit: int = 500) -> list[BrowserDetection]:
        return list(
            self.session.scalars(
                select(BrowserDetection)
                .order_by(BrowserDetection.created_at.desc())
                .limit(limit)
            ).all()
        )

    def remove(self, detection_id: int) -> None:
        detection = self.session.get(BrowserDetection, detection_id)
        if detection is not None:
            self.session.delete(detection)
            self.session.commit()

    def remove_file_everywhere(self, url: str) -> None:
        """Drop *url* from every detection; delete detections left empty.

        The page shows each URL once even when several pages reference it, so
        removing a listed file must clear it from all detections.
        """
        self.remove_urls_everywhere({url})

    def remove_urls_everywhere(self, urls: set[str]) -> None:
        """Drop every *urls* entry from all detections in a single pass.

        Removals are batched: one table scan plus one commit, instead of one
        scan per URL, so "Remove" stays fast when many files are selected.
        """
        if not urls:
            return
        changed = False
        for detection in self.session.scalars(select(BrowserDetection)).all():
            files = [
                f
                for f in (detection.files_json or [])
                if str(f.get("url") or "") not in urls
            ]
            if files:
                if len(files) != len(detection.files_json or []):
                    detection.files_json = files
                    detection.count = len(files)
                    changed = True
            else:
                self.session.delete(detection)
                changed = True
        if changed:
            self.session.commit()


class BrowserRequestRepository:
    """Data-access operations for app->browser requests (e.g. ``blob:`` fetches)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        request_type: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> BrowserRequest:
        request = BrowserRequest(type=request_type, payload_json=payload)
        self.session.add(request)
        self.session.commit()
        self.session.refresh(request)
        return request

    def get(self, request_id: int) -> Optional[BrowserRequest]:
        return self.session.get(BrowserRequest, request_id)

    def next_queued(self) -> Optional[BrowserRequest]:
        """Claim and mark as sent the oldest queued request (host writer)."""
        request = self.session.scalars(
            select(BrowserRequest)
            .where(BrowserRequest.status == "queued")
            .order_by(BrowserRequest.created_at.asc(), BrowserRequest.id.asc())
            .limit(1)
        ).first()
        if request is None:
            return None
        request.status = "sent"
        self.session.commit()
        return request

    def resolve_data(
        self,
        request_id: int,
        *,
        data_base64: str,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        request = self.session.get(BrowserRequest, request_id)
        if request is None:
            return False
        request.status = "ready"
        request.data_base64 = data_base64
        request.result_json = meta or {}
        self.session.commit()
        return True

    def mark_error(self, request_id: int, message: str) -> bool:
        request = self.session.get(BrowserRequest, request_id)
        if request is None:
            return False
        request.status = "error"
        request.result_json = {"message": message}
        self.session.commit()
        return True

    def mark_expired(self, request_id: int) -> bool:
        request = self.session.get(BrowserRequest, request_id)
        if request is None:
            return False
        if request.status in ("queued", "sent"):
            request.status = "expired"
            request.result_json = {"message": "timed out"}
            self.session.commit()
        return True

    def expire_stale(self, max_age_seconds: int = 60) -> int:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        stale = list(
            self.session.scalars(
                select(BrowserRequest).where(
                    BrowserRequest.status.in_(("queued", "sent")),
                    BrowserRequest.created_at < cutoff,
                )
            ).all()
        )
        for request in stale:
            request.status = "expired"
            request.result_json = {"message": "timed out"}
        if stale:
            self.session.commit()
        return len(stale)


class FileHashRepository:
    """Data-access operations for the duplicate-detection file index."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        file_path: str,
        filename: str,
        size: int,
        hash_algo: str,
        hash_value: str,
        *,
        category_id: int | None = None,
    ) -> FileHash:
        entry = FileHash(
            file_path=file_path,
            filename=filename,
            size=size,
            hash_algo=hash_algo,
            hash_value=hash_value,
            category_id=category_id,
        )
        self.session.add(entry)
        self.session.commit()
        self.session.refresh(entry)
        return entry

    def get(self, entry_id: int) -> Optional[FileHash]:
        return self.session.get(FileHash, entry_id)

    def by_hash(self, hash_algo: str, hash_value: str) -> Optional[FileHash]:
        return self.session.scalar(
            select(FileHash).where(
                FileHash.hash_algo == hash_algo,
                FileHash.hash_value == hash_value,
            )
        )

    def by_hash_all(self, hash_algo: str, hash_value: str) -> list[FileHash]:
        return list(
            self.session.scalars(
                select(FileHash).where(
                    FileHash.hash_algo == hash_algo,
                    FileHash.hash_value == hash_value,
                )
            ).all()
        )

    def by_size(self, size: int) -> list[FileHash]:
        """Files of exactly *size* bytes, for a cheap first-pass size check."""
        return list(
            self.session.scalars(
                select(FileHash).where(FileHash.size == size)
            ).all()
        )

    def list(self, limit: int = 500, offset: int = 0) -> list[FileHash]:
        return list(
            self.session.scalars(
                select(FileHash).order_by(FileHash.created_at.desc()).limit(limit).offset(offset)
            ).all()
        )

    def count(self) -> int:
        from sqlalchemy import func

        return int(
            self.session.scalar(select(func.count()).select_from(FileHash)) or 0
        )

    def remove(self, entry: FileHash) -> None:
        self.session.delete(entry)
        self.session.commit()

    def remove_by_file_path(self, file_path: str) -> int:
        entries = list(
            self.session.scalars(
                select(FileHash).where(FileHash.file_path == file_path)
            ).all()
        )
        for entry in entries:
            self.session.delete(entry)
        if entries:
            self.session.commit()
        return len(entries)


class SpeedTestRepository:
    """Data-access operations for network speed test results."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        download_mbps: float | None = None,
        upload_mbps: float | None = None,
        latency_ms: float | None = None,
        server_name: str | None = None,
        server_url: str | None = None,
        isp_name: str | None = None,
        test_size_mb: int = 25,
        error: str | None = None,
    ) -> SpeedTest:
        test = SpeedTest(
            download_mbps=download_mbps,
            upload_mbps=upload_mbps,
            latency_ms=latency_ms,
            server_name=server_name,
            server_url=server_url,
            isp_name=isp_name,
            test_size_mb=test_size_mb,
            error=error,
        )
        self.session.add(test)
        self.session.commit()
        self.session.refresh(test)
        return test

    def largest(self, limit: int = 200) -> list[SpeedTest]:
        return list(
            self.session.scalars(
                select(SpeedTest)
                .order_by(SpeedTest.ts.desc())
                .limit(limit)
            ).all()
        )

    def list(self, limit: int = 500, offset: int = 0) -> list[SpeedTest]:
        return list(
            self.session.scalars(
                select(SpeedTest)
                .order_by(SpeedTest.ts.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )

    def count(self) -> int:
        from sqlalchemy import func

        return int(
            self.session.scalar(select(func.count()).select_from(SpeedTest)) or 0
        )

    def stats(self) -> dict[str, Any]:
        from sqlalchemy import func

        row = self.session.execute(
            select(
                func.count().label("count"),
                func.avg(SpeedTest.download_mbps).label("avg_download"),
                func.max(SpeedTest.download_mbps).label("max_download"),
                func.min(SpeedTest.download_mbps).label("min_download"),
                func.avg(SpeedTest.latency_ms).label("avg_latency"),
                func.max(SpeedTest.latency_ms).label("max_latency"),
            ).select_from(SpeedTest)
        ).one()
        return {
            "count": int(row.count or 0),
            "avg_download_mbps": row.avg_download,
            "max_download_mbps": row.max_download,
            "min_download_mbps": row.min_download,
            "avg_latency_ms": row.avg_latency,
            "max_latency_ms": row.max_latency,
        }

    def remove(self, test: SpeedTest) -> None:
        self.session.delete(test)
        self.session.commit()

    def prune(self, keep: int = 500) -> int:
        rows = self.list(limit=100000)
        stale = rows[keep:]
        for row in stale:
            self.session.delete(row)
        if stale:
            self.session.commit()
        return len(stale)


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
