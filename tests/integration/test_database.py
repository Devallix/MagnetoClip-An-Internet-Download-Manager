from sqlalchemy import inspect

from magnetoclip.database.models import DownloadStatus
from magnetoclip.database.repositories import (
    BrowserRequestRepository,
    DownloadRepository,
    SettingsStore,
)
from magnetoclip.database.session import Database


def test_migrations_create_full_schema(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    inspector = inspect(db.engine)
    for table in (
        "downloads",
        "download_segments",
        "categories",
        "settings",
        "browser_events",
        "download_statistics",
        "proxy_profiles",
        "verified_runs",
        "pending_captures",
        "browser_detections",
        "browser_requests",
        "torrent_search_history",
        "schema_version",
    ):
        assert inspector.has_table(table), f"missing table {table}"
    db.close()


def test_migration_upgrade_adds_browser_tables(tmp_path):
    from sqlalchemy import text

    from magnetoclip.database import migrations

    db = Database(tmp_path / "old.db")
    with db.engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        )
        conn.execute(text("INSERT INTO schema_version (version) VALUES (1)"))
    migrations.MIGRATIONS[0](db.engine)
    db.initialize()
    inspector = inspect(db.engine)
    assert inspector.has_table("pending_captures")
    assert inspector.has_table("browser_detections")
    db.close()


def test_download_crud(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    repo = DownloadRepository(db.Session())

    download = repo.add(
        "https://example.com/file.zip",
        filename="file.zip",
        priority=5,
        connections_max=16,
    )
    assert download.id is not None
    assert download.status == DownloadStatus.queued
    assert download.connections_max == 16

    loaded = repo.get(download.id)
    assert loaded.url == "https://example.com/file.zip"

    repo.update_status(download.id, DownloadStatus.downloading, size_total=1000)
    refreshed = repo.get(download.id)
    assert refreshed.status == DownloadStatus.downloading
    assert refreshed.size_total == 1000

    listed = repo.list(status=DownloadStatus.downloading)
    assert [d.id for d in listed] == [download.id]
    db.close()


def test_settings_store_roundtrip(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    store = SettingsStore(db.Session)
    store.save("appearance.theme", "light")
    store.save_many(
        {"network.timeout_seconds": 60, "downloads.simultaneous": 5}
    )
    loaded = store.load_all()
    assert loaded["appearance.theme"] == "light"
    assert loaded["network.timeout_seconds"] == 60
    assert loaded["downloads.simultaneous"] == 5
    db.close()


def test_schedule_crud_removed(tmp_path):
    """Legacy queue/schedule tables must be dropped by migration 009."""
    db = Database(tmp_path / "test.db")
    db.initialize()
    inspector = inspect(db.engine)
    for table in ("queues", "queue_items", "schedules"):
        assert not inspector.has_table(table), f"table {table} should be gone"
    columns = {c["name"] for c in inspector.get_columns("downloads")}
    assert "queue_id" not in columns
    db.close()


def test_browser_request_repository_lifecycle(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    repo = BrowserRequestRepository(db.Session())

    request = repo.add(
        "fetch_blob", payload={"url": "blob:https://web.telegram.org/uuid"}
    )
    assert request.id is not None
    assert request.status == "queued"

    claimed = repo.next_queued()
    assert claimed.id == request.id
    assert claimed.status == "sent"
    assert repo.next_queued() is None

    assert repo.resolve_data(request.id, data_base64="Y2F0", meta={"mime_type": "image/png"})
    loaded = repo.get(request.id)
    assert loaded.status == "ready"
    assert loaded.data_base64 == "Y2F0"
    assert loaded.result_json["mime_type"] == "image/png"
    db.close()


def test_browser_request_repository_errors_and_expiry(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    repo = BrowserRequestRepository(db.Session())
    request = repo.add("fetch_blob", payload={"url": "blob:https://x/y"})

    assert repo.mark_error(request.id, "no page open")
    assert repo.get(request.id).status == "error"
    assert repo.get(request.id).result_json["message"] == "no page open"

    second = repo.add("fetch_blob", payload={"url": "blob:https://x/z"})
    assert repo.mark_expired(second.id)
    assert repo.get(second.id).status == "expired"

    # Expiring a finished request leaves its state untouched.
    assert repo.mark_expired(request.id)
    assert repo.get(request.id).status == "error"
    db.close()
