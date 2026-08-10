"""Aggregations powering the analytics dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from magnetoclip.database.models import (
    Category,
    Download,
    DownloadStatistic,
    DownloadStatus,
)


class DashboardService:
    """Read-side queries over download and statistics tables."""

    def __init__(self, context) -> None:
        self.session_factory = context.session_factory

    # ----- overview -----

    def overview(self) -> dict:
        with self.session_factory() as session:
            total = session.scalar(select(func.count(Download.id))) or 0
            completed = (
                session.scalar(
                    select(func.count(Download.id)).where(
                        Download.status == DownloadStatus.completed
                    )
                )
                or 0
            )
            failed = (
                session.scalar(
                    select(func.count(Download.id)).where(
                        Download.status.in_(
                            (DownloadStatus.failed, DownloadStatus.verification_failed)
                        )
                    )
                )
                or 0
            )
            bytes_downloaded = (
                session.scalar(
                    select(func.sum(Download.size_downloaded)).where(
                        Download.status == DownloadStatus.completed
                    )
                )
                or 0
            )
            avg_speed = session.scalar(select(func.avg(Download.speed_avg))) or 0.0
            peak_speed = session.scalar(select(func.max(Download.speed_peak))) or 0.0
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "bytes_downloaded": bytes_downloaded,
            "avg_speed": float(avg_speed),
            "peak_speed": float(peak_speed),
        }

    # ----- daily activity -----

    def daily_activity(self, days: int = 14) -> list[dict]:
        """Per-day download count and bytes, oldest first, for the last days."""
        start = datetime.now(UTC).date() - timedelta(days=days - 1)
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    func.date(Download.created_at),
                    func.count(Download.id),
                    func.sum(Download.size_downloaded),
                )
                .where(func.date(Download.created_at) >= start.isoformat())
                .group_by(func.date(Download.created_at))
                .order_by(func.date(Download.created_at))
            ).all()
        by_day = {str(day): {"count": count, "bytes": bytes_ or 0} for day, count, bytes_ in rows}
        return [
            {"date": (start + timedelta(days=i)).isoformat(), **by_day.get((start + timedelta(days=i)).isoformat(), {"count": 0, "bytes": 0})}
            for i in range(days)
        ]

    def bandwidth_history(self, days: int = 14) -> list[dict]:
        """Per-day bandwidth consumed (bytes) from statistics samples."""
        start = datetime.now(UTC).date() - timedelta(days=days - 1)
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    func.date(DownloadStatistic.ts),
                    func.sum(DownloadStatistic.bandwidth_used),
                )
                .where(func.date(DownloadStatistic.ts) >= start.isoformat())
                .group_by(func.date(DownloadStatistic.ts))
                .order_by(func.date(DownloadStatistic.ts))
            ).all()
        by_day = {str(day): bytes_ or 0 for day, bytes_ in rows}
        return [
            {"date": (start + timedelta(days=i)).isoformat(), "bytes": by_day.get((start + timedelta(days=i)).isoformat(), 0)}
            for i in range(days)
        ]

    def speed_history(self, download_id: int, limit: int = 120) -> list[dict]:
        """Recent speed samples for one download, oldest first."""
        with self.session_factory() as session:
            rows = session.execute(
                select(DownloadStatistic.ts, DownloadStatistic.speed)
                .where(DownloadStatistic.download_id == download_id)
                .order_by(DownloadStatistic.ts.desc())
                .limit(limit)
            ).all()
        return [
            {"ts": ts.strftime("%H:%M:%S"), "speed": speed or 0.0} for ts, speed in reversed(rows)
        ]

    # ----- categories -----

    def category_breakdown(self) -> list[dict]:
        with self.session_factory() as session:
            rows = session.execute(
                select(Category.name, func.count(Download.id), func.sum(Download.size_downloaded))
                .join(Download, Download.category_id == Category.id)
                .group_by(Category.name)
                .order_by(func.count(Download.id).desc())
            ).all()
        return [
            {"name": name, "count": count, "bytes": bytes_ or 0} for name, count, bytes_ in rows
        ]

    def total_by_category(self) -> dict[str, int]:
        return {row["name"]: row["count"] for row in self.category_breakdown()}

    # ----- status -----

    def status_counts(self) -> dict[str, int]:
        with self.session_factory() as session:
            rows = session.execute(
                select(Download.status, func.count(Download.id)).group_by(Download.status)
            ).all()
        return {status.value: count for status, count in rows}
