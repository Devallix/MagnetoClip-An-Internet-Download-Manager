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


def _migration_010_drop_download_rules(engine: Engine) -> None:
    """Remove the download rules table (feature removed)."""
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE IF EXISTS download_rules"))
            conn.commit()
    except Exception:
        pass


def _migration_011_file_hashes(engine: Engine) -> None:
    """Add the file hashes table for duplicate detection."""
    from .models import FileHash

    FileHash.__table__.create(engine, checkfirst=True)


def _migration_012_schedule_rules(engine: Engine) -> None:
    """Legacy weekly download windows (retained for version numbering)."""
    # table is no longer created; migration 014 drops it from existing DBs
    return


def _migration_013_speed_tests(engine: Engine) -> None:
    """Add the speed test results table."""
    from .models import SpeedTest

    SpeedTest.__table__.create(engine, checkfirst=True)


def _migration_014_drop_schedule_rules(engine: Engine) -> None:
    """Drop the weekly download-window table (feature removed)."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS schedule_rules"))


def _migration_015_pending_capture_ua(engine: Engine) -> None:
    """Add browser User-Agent storage so stream downloads can replay the exact
    UA a Cloudflare ``cf_clearance`` cookie is bound to (guarded)."""
    columns = {column["name"] for column in inspect(engine).get_columns("pending_captures")}
    with engine.begin() as conn:
        if "user_agent" not in columns:
            conn.execute(
                text("ALTER TABLE pending_captures ADD COLUMN user_agent VARCHAR(512)")
            )


def _migration_016_stream_context(engine: Engine) -> None:
    """Add stream request-context storage (guarded).

    ``downloads.stream_context_json`` stores capture-time RequestContext extras
    (page/frame URL, host cookies, capture timestamp) that Drive the HLS
    pipeline; ``error_code`` carries the categorized streaming error taxonomy.
    ``pending_captures.page_url``/``frame_url`` pin the tab/frame that issued a
    detected manifest so the download can re-use the correct browser context.
    """
    download_columns = {c["name"] for c in inspect(engine).get_columns("downloads")}
    capture_columns = {c["name"] for c in inspect(engine).get_columns("pending_captures")}
    with engine.begin() as conn:
        if "stream_context_json" not in download_columns:
            conn.execute(
                text("ALTER TABLE downloads ADD COLUMN stream_context_json JSON")
            )
        if "error_code" not in download_columns:
            conn.execute(text("ALTER TABLE downloads ADD COLUMN error_code VARCHAR(64)"))
        if "page_url" not in capture_columns:
            conn.execute(
                text("ALTER TABLE pending_captures ADD COLUMN page_url VARCHAR(4096)")
            )
        if "frame_url" not in capture_columns:
            conn.execute(
                text("ALTER TABLE pending_captures ADD COLUMN frame_url VARCHAR(4096)")
            )


def _migration_018_youtube_quality(engine: Engine) -> None:
    """Add per-download YouTube quality columns (guarded).

    Stores the requested format selection and the actual resolution captured
    after download, so queue rows can show what was asked for versus what the
    platform actually served (the ``[height<=N]`` 'lower if unavailable'
    fallback). ``playlist_items`` gains the per-video requested/actual pair.
    """
    download_columns = {c["name"] for c in inspect(engine).get_columns("downloads")}
    item_columns = {c["name"] for c in inspect(engine).get_columns("playlist_items")}
    with engine.begin() as conn:
        add = [
            ("youtube_quality", "VARCHAR(32)"),
            ("youtube_height", "INTEGER"),
            ("youtube_width", "INTEGER"),
            ("youtube_fps", "INTEGER"),
            ("youtube_format", "VARCHAR(128)"),
            ("youtube_format_selector", "VARCHAR(512)"),
            ("youtube_merge_output_format", "VARCHAR(32)"),
            ("youtube_format_label", "VARCHAR(256)"),
            ("youtube_requested_quality", "VARCHAR(64)"),
            ("actual_width", "INTEGER"),
            ("actual_height", "INTEGER"),
            ("actual_fps", "REAL"),
        ]
        for name, ddl in add:
            if name not in download_columns:
                conn.execute(text(f"ALTER TABLE downloads ADD COLUMN {name} {ddl}"))
        if "requested_quality" not in item_columns:
            conn.execute(
                text(
                    "ALTER TABLE playlist_items "
                    "ADD COLUMN requested_quality VARCHAR(64)"
                )
            )
        if "actual_quality" not in item_columns:
            conn.execute(
                text("ALTER TABLE playlist_items ADD COLUMN actual_quality VARCHAR(64)")
            )


def _migration_017_playlists(engine: Engine) -> None:
    """Add playlist tables and playlist grouping columns (guarded).

    Creates ``playlists`` / ``playlist_items`` and, on ``downloads``, the three
    optional columns used to group queue rows by their parent playlist.
    """
    from .models import Playlist, PlaylistItem

    Playlist.__table__.create(engine, checkfirst=True)
    PlaylistItem.__table__.create(engine, checkfirst=True)

    columns = {column["name"] for column in inspect(engine).get_columns("downloads")}
    with engine.begin() as conn:
        if "playlist_id" not in columns:
            conn.execute(
                text("ALTER TABLE downloads ADD COLUMN playlist_id VARCHAR(128)")
            )
        if "playlist_title" not in columns:
            conn.execute(
                text("ALTER TABLE downloads ADD COLUMN playlist_title VARCHAR(1024)")
            )
        if "playlist_item_index" not in columns:
            conn.execute(
                text("ALTER TABLE downloads ADD COLUMN playlist_item_index INTEGER")
            )


def _migration_019_pending_capture_headers(engine: Engine) -> None:
    """Persist captured browser request headers for protected HLS manifests."""
    columns = {column["name"] for column in inspect(engine).get_columns("pending_captures")}
    with engine.begin() as conn:
        if "headers_json" not in columns:
            conn.execute(text("ALTER TABLE pending_captures ADD COLUMN headers_json JSON"))


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
    _migration_010_drop_download_rules,
    _migration_011_file_hashes,
    _migration_012_schedule_rules,
    _migration_013_speed_tests,
    _migration_014_drop_schedule_rules,
    _migration_015_pending_capture_ua,
    _migration_016_stream_context,
    _migration_017_playlists,
    _migration_018_youtube_quality,
    _migration_019_pending_capture_headers,
]


def run_migrations(engine: Engine) -> None:
    """Run any pending migrations, tracking the applied schema version."""
    _ensure_version_table(engine)
    current = _current_version(engine)
    for index in range(current + 1, len(MIGRATIONS) + 1):
        MIGRATIONS[index - 1](engine)
        _set_version(engine, index)
