from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DownloadStatus(str, enum.Enum):
    queued = "queued"
    scheduled = "scheduled"
    connecting = "connecting"
    downloading = "downloading"
    paused = "paused"
    retrying = "retrying"
    verifying = "verifying"
    completed = "completed"
    failed = "failed"
    verification_failed = "verification_failed"
    stopped = "stopped"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    folder: Mapped[Optional[str]] = mapped_column(String(4096))
    icon: Mapped[Optional[str]] = mapped_column(String(128))
    color: Mapped[Optional[str]] = mapped_column(String(16))
    rules_json: Mapped[dict] = mapped_column(JSON, default=dict)

    downloads: Mapped[list["Download"]] = relationship(back_populates="category")


class ProxyProfile(Base):
    __tablename__ = "proxy_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), default="direct")
    host: Mapped[Optional[str]] = mapped_column(String(512))
    port: Mapped[Optional[int]] = mapped_column(Integer)
    username_ref: Mapped[Optional[str]] = mapped_column(String(512))

    downloads: Mapped[list["Download"]] = relationship(back_populates="proxy_profile")


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(4096), nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String(1024))
    save_path: Mapped[Optional[str]] = mapped_column(String(4096))
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"))
    size_total: Mapped[Optional[int]] = mapped_column(BigInteger)
    size_downloaded: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[DownloadStatus] = mapped_column(
        Enum(DownloadStatus), default=DownloadStatus.queued
    )
    speed_avg: Mapped[Optional[float]] = mapped_column(Float)
    speed_peak: Mapped[Optional[float]] = mapped_column(Float)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    connections_max: Mapped[int] = mapped_column(Integer, default=8)
    connections_active: Mapped[int] = mapped_column(Integer, default=0)
    proxy_profile_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("proxy_profiles.id")
    )
    headers_json: Mapped[Optional[dict]] = mapped_column(JSON)
    auth_ref: Mapped[Optional[str]] = mapped_column(String(512))
    etag: Mapped[Optional[str]] = mapped_column(String(512))
    last_modified: Mapped[Optional[str]] = mapped_column(String(64))
    hash_algo: Mapped[Optional[str]] = mapped_column(String(32))
    hash_expected: Mapped[Optional[str]] = mapped_column(String(128))
    hash_calculated: Mapped[Optional[str]] = mapped_column(String(128))
    detected_type: Mapped[Optional[str]] = mapped_column(String(64))
    media_metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    error: Mapped[Optional[str]] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    # Torrent-specific columns
    torrent_info_hash: Mapped[Optional[str]] = mapped_column(String(40))
    torrent_num_peers: Mapped[Optional[int]] = mapped_column(Integer)
    torrent_num_seeds: Mapped[Optional[int]] = mapped_column(Integer)
    torrent_num_pieces: Mapped[Optional[int]] = mapped_column(Integer)
    torrent_piece_size: Mapped[Optional[int]] = mapped_column(Integer)
    torrent_sequential: Mapped[bool] = mapped_column(Boolean, default=False)
    torrent_seeding: Mapped[bool] = mapped_column(Boolean, default=False)

    category: Mapped[Optional[Category]] = relationship(back_populates="downloads")
    proxy_profile: Mapped[Optional[ProxyProfile]] = relationship(
        back_populates="downloads"
    )
    segments: Mapped[list["DownloadSegment"]] = relationship(
        back_populates="download", cascade="all, delete-orphan"
    )
    stats: Mapped[list["DownloadStatistic"]] = relationship(
        back_populates="download", cascade="all, delete-orphan"
    )
    verified_runs: Mapped[list["VerifiedRun"]] = relationship(
        back_populates="download", cascade="all, delete-orphan"
    )


class DownloadSegment(Base):
    __tablename__ = "download_segments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    download_id: Mapped[int] = mapped_column(
        ForeignKey("downloads.id"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_byte: Mapped[Optional[int]] = mapped_column(BigInteger)
    end_byte: Mapped[Optional[int]] = mapped_column(BigInteger)
    downloaded: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[Optional[str]] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    download: Mapped[Download] = relationship(back_populates="segments")


class DownloadStatistic(Base):
    __tablename__ = "download_statistics"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    download_id: Mapped[int] = mapped_column(
        ForeignKey("downloads.id"), nullable=False, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    speed: Mapped[Optional[float]] = mapped_column(Float)
    connections: Mapped[int] = mapped_column(Integer, default=0)
    bandwidth_used: Mapped[Optional[int]] = mapped_column(BigInteger)

    download: Mapped[Download] = relationship(back_populates="stats")


class VerifiedRun(Base):
    __tablename__ = "verified_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    download_id: Mapped[int] = mapped_column(
        ForeignKey("downloads.id"), nullable=False, index=True
    )
    algo: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # matched / mismatch
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    download: Mapped[Download] = relationship(back_populates="verified_runs")


class BrowserEvent(Base):
    __tablename__ = "browser_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[Optional[str]] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(4096), nullable=False)
    detected_type: Mapped[Optional[str]] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PendingCapture(Base):
    """A capture from the browser awaiting user confirmation in the app.

    The native-messaging host runs in a separate process and cannot show Qt
    dialogs, so captures are persisted here and the main window's capture
    watcher presents them to the user.
    """

    __tablename__ = "pending_captures"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(4096), nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String(1024))
    referrer: Mapped[Optional[str]] = mapped_column(String(4096))
    source: Mapped[Optional[str]] = mapped_column(String(64))
    detected_type: Mapped[Optional[str]] = mapped_column(String(64))
    cookies_json: Mapped[Optional[dict]] = mapped_column(JSON)
    data_base64: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    download_id: Mapped[Optional[int]] = mapped_column(ForeignKey("downloads.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BrowserDetection(Base):
    """A set of downloadable files found by the extension on a web page."""

    __tablename__ = "browser_detections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    page_url: Mapped[str] = mapped_column(String(4096), nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0)
    files_json: Mapped[Optional[dict]] = mapped_column(JSON)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BrowserRequest(Base):
    """A request from the app for the browser to fulfil (e.g. a ``blob:`` fetch).

    The native-messaging host runs in a separate process and can only answer
    extension messages, so the app persists a request here and the host's
    outbound writer pushes it to the extension. The extension streams the blob
    back as chunks, which the host reassembles into this row for the app to read.
    """

    __tablename__ = "browser_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued"
    )
    result_json: Mapped[Optional[dict]] = mapped_column(JSON)
    data_base64: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[Optional[object]] = mapped_column(JSON)


class TorrentSearchHistory(Base):
    __tablename__ = "torrent_search_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    site: Mapped[str] = mapped_column(String(64), nullable=False)
    results_json: Mapped[Optional[dict]] = mapped_column(JSON)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
