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


MIGRATIONS: list[Migration] = [
    _migration_001_create_all,
    _migration_002_media_columns,
    _migration_003_browser_tables,
]


def run_migrations(engine: Engine) -> None:
    """Run any pending migrations, tracking the applied schema version."""
    _ensure_version_table(engine)
    current = _current_version(engine)
    for index in range(current + 1, len(MIGRATIONS) + 1):
        MIGRATIONS[index - 1](engine)
        _set_version(engine, index)
