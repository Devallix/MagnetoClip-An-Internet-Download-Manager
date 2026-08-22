from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Column, Integer, MetaData, Table, inspect, text
from sqlalchemy.engine import Engine

from .models import Base

SCHEMA_VERSION_TABLE = "schema_version"

Migration = Callable[[Engine], None]


def _ensure_version_table(engine: Engine) -> None:
    metadata = MetaData()
    Table(
        SCHEMA_VERSION_TABLE,
        metadata,
        Column("version", Integer, primary_key=True),
    )
    metadata.create_all(engine)


def _current_version(engine: Engine) -> int:
    with engine.connect() as conn:
        version = conn.execute(
            text(f"SELECT COALESCE(MAX(version), 0) FROM {SCHEMA_VERSION_TABLE}")
        ).scalar_one()
    return int(version)


def _set_version(engine: Engine, version: int) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {SCHEMA_VERSION_TABLE}"))
        conn.execute(
            text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (:v)"),
            {"v": version},
        )


def _migration_001_create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def _migration_002_media_columns(engine: Engine) -> None:
    """Add media detection columns to the downloads table (guarded)."""
    columns = {column["name"] for column in inspect(engine).get_columns("downloads")}
    with engine.begin() as conn:
        if "detected_type" not in columns:
            conn.execute(
                text("ALTER TABLE downloads ADD COLUMN detected_type VARCHAR(64)")
            )
        if "media_metadata_json" not in columns:
            conn.execute(
                text("ALTER TABLE downloads ADD COLUMN media_metadata_json JSON")
            )


def _migration_003_browser_tables(engine: Engine) -> None:
    """Add tables used by browser capture confirmation and page scanning."""
    from .models import BrowserDetection, PendingCapture

    PendingCapture.__table__.create(engine, checkfirst=True)
    BrowserDetection.__table__.create(engine, checkfirst=True)


def _migration_004_pending_capture_cookies(engine: Engine) -> None:
    """Add cookie storage for browser captures (guarded)."""
    columns = {column["name"] for column in inspect(engine).get_columns("pending_captures")}
    with engine.begin() as conn:
        if "cookies_json" not in columns:
            conn.execute(
                text("ALTER TABLE pending_captures ADD COLUMN cookies_json JSON")
            )


def _migration_005_pending_capture_data(engine: Engine) -> None:
    """Add inline data for browser captures (blob: media, e.g. Telegram)."""
    columns = {column["name"] for column in inspect(engine).get_columns("pending_captures")}
    with engine.begin() as conn:
        if "data_base64" not in columns:
            conn.execute(
                text("ALTER TABLE pending_captures ADD COLUMN data_base64 TEXT")
            )


def _migration_006_browser_requests(engine: Engine) -> None:
    """Add the app->browser request table (blob: URL fetches)."""
    from .models import BrowserRequest

    BrowserRequest.__table__.create(engine, checkfirst=True)


def _migration_007_torrent_columns(engine: Engine) -> None:
    """Add torrent-specific columns to the downloads table (guarded)."""
    columns = {column["name"] for column in inspect(engine).get_columns("downloads")}
    with engine.begin() as conn:
        torrent_columns = [
            ("torrent_info_hash", "VARCHAR(40)"),
            ("torrent_num_peers", "INTEGER"),
            ("torrent_num_seeds", "INTEGER"),
            ("torrent_num_pieces", "INTEGER"),
            ("torrent_piece_size", "INTEGER"),
            ("torrent_sequential", "BOOLEAN DEFAULT 0"),
            ("torrent_seeding", "BOOLEAN DEFAULT 0"),
        ]
        for col_name, col_type in torrent_columns:
            if col_name not in columns:
                conn.execute(
                    text(f"ALTER TABLE downloads ADD COLUMN {col_name} {col_type}")
                )


def _migration_008_torrent_search_history(engine: Engine) -> None:
    """Add the torrent search history table."""
    from .models import TorrentSearchHistory

    TorrentSearchHistory.__table__.create(engine, checkfirst=True)


def _migration_009_drop_queue_tables(engine: Engine) -> None:
    """Drop named queues/schedules (replaced by the global torrent queue)."""
    names = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for table in ("queue_items", "queues", "schedules"):
            if table in names:
                conn.execute(text(f"DROP TABLE {table}"))
    columns = {column["name"] for column in inspect(engine).get_columns("downloads")}
    if "queue_id" not in columns:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE downloads DROP COLUMN queue_id"))
    except Exception:  # noqa: BLE001 - older SQLite cannot drop columns
        # The column is no longer mapped by the model, so leaving it in
        # place is harmless.
        pass


MIGRATIONS: list[Migration] = [
    _migration_001_create_all,
    _migration_002_media_columns,
    _migration_003_browser_tables,
    _migration_004_pending_capture_cookies,
    _migration_005_pending_capture_data,
    _migration_006_browser_requests,
    _migration_007_torrent_columns,
    _migration_008_torrent_search_history,
    _migration_009_drop_queue_tables,
]


def run_migrations(engine: Engine) -> None:
    """Run any pending migrations, tracking the applied schema version."""
    _ensure_version_table(engine)
    current = _current_version(engine)
    for index in range(current + 1, len(MIGRATIONS) + 1):
        MIGRATIONS[index - 1](engine)
        _set_version(engine, index)
