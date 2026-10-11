"""Tests for the browser integration bridge and installer."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.browser.integration.install import (
    build_host_manifest,
    ensure_extension_key,
    extension_id_from_public_key,
    host_manifest_path,
)
from magnetoclip.browser.manager import BrowserManager
from magnetoclip.browser.skip import enable_skip_all, skip_all_active
from magnetoclip.core.events.bus import Events
from magnetoclip.database.models import DownloadStatus
from tests.support.http_server import PayloadServer, html_server


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    context.settings.set("browser.integration_enabled", True)
    return context


# ----- bridge -----


def test_ping_pong(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    assert bridge.handle_message({"type": "ping"}) == {"type": "pong"}
    asyncio.run(context.shutdown())


def test_unknown_type_errors(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "nope"})
    assert response["type"] == "error"
    asyncio.run(context.shutdown())


def test_capture_rejected_when_integration_disabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "capture", "url": "https://example.com/x"})
    assert response["type"] == "capture_error"
    asyncio.run(context.shutdown())


def test_archive_rejected_when_integration_disabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {"type": "archive", "url": "https://example.com/page", "source": "context_menu"}
    )
    assert response["type"] == "archive_error"
    asyncio.run(context.shutdown())


def test_archive_rejects_non_http_url(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {"type": "archive", "url": "file:///C:/tmp/x.html", "source": "context_menu"}
    )
    assert response["type"] == "archive_error"
    asyncio.run(context.shutdown())


def test_archive_queues_webpage_download(tmp_path):
    context = make_context(tmp_path)
    # The archiver would fetch the URL on a background thread; keep the unit
    # test offline by short-circuiting the archive kick-off.
    context.manager._run_archiving = lambda *args, **kwargs: None
    bridge = BrowserManager(context)
    posted: list[dict] = []
    context.events.connect(Events.BROWSER_EVENT, posted.append)

    response = bridge.handle_message(
        {
            "type": "archive",
            "url": "https://example.com/article",
            "cookies": "session=abc",
            "referrer": "https://example.com/article",
            "source": "context_menu",
            "detected_type": "webpage",
        }
    )
    assert response["type"] == "archive_ok"
    download_id = response["download_id"]

    download = context.manager.get_download(download_id)
    assert download is not None
    assert download.detected_type == "webpage"
    assert download.url == "https://example.com/article"
    snapshot = context.manager.snapshot_item(download)
    assert snapshot["status"] == "queued"

    assert len(posted) == 1
    assert posted[0]["download_id"] == download_id
    assert posted[0].get("archive") is True
    asyncio.run(context.shutdown())


# ----- detection queue sync -----


def _detection(url, **overrides):
    entry = {
        "id": "abc123",
        "url": url,
        "filename": url.rsplit("/", 1)[-1],
        "category": "videos",
        "detected_type": "video",
        "mime_type": "video/mp4",
        "page_url": "https://example.com/watch",
        "source": "network_capture",
    }
    entry.update(overrides)
    return entry


def _stored_files(context, page_url="https://example.com/watch"):
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserDetectionRepository

        detections = BrowserDetectionRepository(session).list_detections()
    for detection in detections:
        if detection.page_url == page_url:
            return detection.files_json
    return []


def test_detection_sync_stores_queued_entries(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    posted: list[dict] = []
    context.events.connect(Events.BROWSER_EVENT, posted.append)

    response = bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [
                _detection("https://cdn.example.com/a.mp4"),
                _detection(
                    "https://cdn.example.com/b.zip",
                    id="def456",
                    category="compressed",
                    detected_type="archive",
                    mime_type="application/zip",
                ),
            ],
        }
    )
    assert response["type"] == "detection_sync_ok"
    assert response["received"] == 2
    assert response["added"] == 2

    files = _stored_files(context)
    by_url = {f["url"]: f for f in files}
    assert by_url["https://cdn.example.com/a.mp4"]["category"] == "videos"
    assert by_url["https://cdn.example.com/b.zip"]["category"] == "compressed"
    assert by_url["https://cdn.example.com/b.zip"]["detected_type"] == "archive"
    # A queued detection is never a download.
    assert context.manager.list_snapshots(limit=10) == []
    assert [p["source"] for p in posted] == ["detection_sync"]
    asyncio.run(context.shutdown())


def test_detection_sync_deduplicates_repeat_sends(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    batch = {"type": "detection_sync", "detections": [_detection("https://cdn.example.com/a.mp4")]}

    first = bridge.handle_message(batch)
    assert first["added"] == 1
    second = bridge.handle_message(batch)
    assert second["added"] == 0
    assert len(_stored_files(context)) == 1
    asyncio.run(context.shutdown())


def test_detection_sync_merges_into_the_page_row(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [_detection("https://cdn.example.com/a.mp4")],
        }
    )
    response = bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [
                _detection("https://cdn.example.com/b.mp4", id="zzz", page_url="https://x.test/p")
            ],
        }
    )
    assert response["pages"] == 1
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserDetectionRepository

        detections = BrowserDetectionRepository(session).list_detections()
    assert len(detections) == 2
    # Same page -> same row, both files.
    assert len(_stored_files(context)) == 1
    assert len(_stored_files(context, "https://x.test/p")) == 1
    asyncio.run(context.shutdown())


def test_detection_sync_keeps_stream_request_context(tmp_path):
    """A manifest must survive the queue with the context the player used.

    Segment/key requests are replayed from the download row; if the sync step
    drops the headers, cookies or player frame, the CDN answers 403 and the
    stream dies after the manifest was already saved.
    """
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [
                _detection(
                    "https://cdn.example.com/hls/master.m3u8",
                    category="videos",
                    detected_type="video",
                    mime_type="application/vnd.apple.mpegurl",
                    headers={"Origin": "https://player.example.com", "Sec-Fetch-Mode": "cors"},
                    cookies="sid=1; cf_clearance=abc",
                    user_agent="Mozilla/5.0 (X11; Linux x86_64)",
                    referrer="https://player.example.com/embed/9",
                    frame_url="https://player.example.com/embed/9",
                )
            ],
        }
    )
    files = _stored_files(context)
    assert len(files) == 1
    entry = files[0]
    assert entry["headers"] == {
        "Origin": "https://player.example.com",
        "Sec-Fetch-Mode": "cors",
    }
    assert entry["cookies"] == {"sid": "1", "cf_clearance": "abc"}
    assert entry["user_agent"] == "Mozilla/5.0 (X11; Linux x86_64)"
    assert entry["referrer"] == "https://player.example.com/embed/9"
    assert entry["frame_url"] == "https://player.example.com/embed/9"
    asyncio.run(context.shutdown())


def test_detection_sync_derives_frame_url_from_referrer(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [
                _detection(
                    "https://cdn.example.com/hls/v.m3u8",
                    referrer="https://player.example.com/embed/7",
                    frame_url="",
                    headers={},
                )
            ],
        }
    )
    entry = _stored_files(context)[0]
    assert entry["frame_url"] == "https://player.example.com/embed/7"
    asyncio.run(context.shutdown())


def test_detection_sync_enriches_existing_entries(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [_detection("https://cdn.example.com/a.mp4")],
        }
    )
    bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [
                _detection("https://cdn.example.com/a.mp4", cookies="sid=1", filename="")
            ],
        }
    )
    files = _stored_files(context)
    assert len(files) == 1
    # Stored as a mapping, not the raw header string: the download manager
    # merges cookies by name and raises if it is handed a string.
    assert files[0]["cookies"] == {"sid": "1"}
    asyncio.run(context.shutdown())


def test_detection_sync_drops_unresolvable_blob_uris(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [
                _detection("blob:https://example.com/1234", page_url="https://example.com/watch"),
                _detection("https://cdn.example.com/ok.mp4"),
            ],
        }
    )
    assert response["stored"] == 1
    assert [f["url"] for f in _stored_files(context)] == ["https://cdn.example.com/ok.mp4"]
    asyncio.run(context.shutdown())


def test_detection_sync_keeps_inline_blob_data(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [
                _detection(
                    "blob:https://example.com/1234",
                    data_base64="aGk=",
                    page_url="https://example.com/watch",
                )
            ],
        }
    )
    files = _stored_files(context)
    assert len(files) == 1
    assert files[0]["data_base64"] == "aGk="
    asyncio.run(context.shutdown())


def test_detection_sync_rejects_unknown_category(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.handle_message(
        {
            "type": "detection_sync",
            "detections": [_detection("https://cdn.example.com/a.bin", category="nope")],
        }
    )
    assert _stored_files(context)[0]["category"] == ""
    asyncio.run(context.shutdown())


def test_detection_sync_requires_a_list(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    assert bridge.handle_message({"type": "detection_sync"})["type"] == "detection_sync_error"
    assert (
        bridge.handle_message({"type": "detection_sync", "detections": []})["type"]
        == "detection_sync_ok"
    )
    asyncio.run(context.shutdown())


def test_detection_sync_rejected_when_integration_disabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {"type": "detection_sync", "detections": [_detection("https://cdn.example.com/a.mp4")]}
    )
    assert response["type"] == "detection_sync_error"
    asyncio.run(context.shutdown())


def test_open_detected_queues_navigation_for_the_app_process(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "open_detected", "category": "compressed"})
    assert response == {"type": "open_detected_ok", "category": "compressed"}
    # The host process shares the database, not the UI's event bus.
    with context.session_factory() as session:
        from magnetoclip.database.models import BrowserRequest

        rows = session.query(BrowserRequest).filter(BrowserRequest.type == "navigate").all()
    assert len(rows) == 1
    assert rows[0].payload_json == {"page": "detected", "category": "compressed"}
    asyncio.run(context.shutdown())


def test_open_detected_without_category_shows_everything(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    assert bridge.handle_message({"type": "open_detected"})["category"] == ""
    with context.session_factory() as session:
        from magnetoclip.database.models import BrowserRequest

        row = session.query(BrowserRequest).filter(BrowserRequest.type == "navigate").one()
    assert row.payload_json["category"] == ""
    asyncio.run(context.shutdown())


def test_navigation_requests_are_not_pushed_to_the_extension(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.handle_message({"type": "open_detected", "category": "audio"})
    # The writer thread only speaks to the browser; a "navigate" row belongs to
    # the UI poll and must never be claimed here.
    assert bridge.next_outbound_message() is None
    asyncio.run(context.shutdown())


# ----- outbound detection messages -----


def test_outbound_detections_handled(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        BrowserRequestRepository(session).add(
            "detections_handled", payload={"urls": ["https://cdn.example.com/a.mp4"], "ids": ["x"]}
        )
    assert bridge.next_outbound_message() == {
        "type": "detections_handled",
        "urls": ["https://cdn.example.com/a.mp4"],
        "ids": ["x"],
    }
    # Claimed exactly once.
    assert bridge.next_outbound_message() is None
    asyncio.run(context.shutdown())


def test_outbound_fetch_detections(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        request = BrowserRequestRepository(session).add("fetch_detections")
    assert bridge.next_outbound_message() == {
        "type": "fetch_detections",
        "request_id": request.id,
    }
    asyncio.run(context.shutdown())


def test_outbound_handled_without_urls_is_marked_error(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        repo = BrowserRequestRepository(session)
        repo.add("detections_handled", payload={"urls": []})
    assert bridge.next_outbound_message() is None
    with context.session_factory() as session:
        from magnetoclip.database.models import BrowserRequest

        assert session.get(BrowserRequest, 1).status == "error"
    asyncio.run(context.shutdown())


def test_outbound_disabled_when_integration_off(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", False)
    bridge = BrowserManager(context)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        BrowserRequestRepository(session).add("fetch_detections")
    assert bridge.next_outbound_message() is None
    asyncio.run(context.shutdown())


def test_settings_reports_flags(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.capture_enabled", False)
    context.settings.set("browser.default_downloader", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "settings"})
    assert response["type"] == "settings_ok"
    assert response["integration_enabled"] is True
    assert response["capture_enabled"] is False
    assert response["default_downloader"] is True
    asyncio.run(context.shutdown())


@pytest.mark.asyncio
async def test_capture_creates_and_starts_download(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    context.settings.set("browser.default_downloader", True)
    payload = b"x" * 4096
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    posted: list[dict] = []
    context.events.connect(Events.BROWSER_EVENT, posted.append)

    with PayloadServer(payload) as server:
        response = bridge.handle_message(
            {
                "type": "capture",
                "url": server.url,
                "filename": "captured.bin",
                "referrer": "https://example.com/page",
                "source": "extension",
            }
        )
        assert response["type"] == "capture_ok"
        download_id = response["download_id"]
        deadline = asyncio.get_running_loop().time() + 15
        status = None
        final_snapshot = None
        while asyncio.get_running_loop().time() < deadline:
            download = context.manager.get_download(download_id)
            if download is None:
                break
            final_snapshot = context.manager.snapshot_item(download)
            status = final_snapshot["status"]
            if status in ("completed", "failed", "verification_failed"):
                break
            await asyncio.sleep(0.02)
        assert status == "completed", f"expected completed, got {status}"
        assert Path(final_snapshot["save_path"]).exists()

    assert len(posted) == 1
    assert posted[0]["download_id"] == download_id
    await context.shutdown()


@pytest.mark.asyncio
async def test_capture_probe_rejects_html_error_page(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    html = b"<html><head><title>Sign in</title></head><body>login</body></html>"
    with html_server(html) as server:
        response = await asyncio.to_thread(
            bridge.handle_message,
            {
                "type": "capture",
                "url": server.url,
                "filename": "video.mp4",
                "source": "extension",
            },
        )
    assert response["type"] == "capture_error"
    assert "HTML" in response["message"]
    await context.shutdown()


@pytest.mark.asyncio
async def test_capture_probe_rejects_http_error(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    with PayloadServer(b"x" * 100) as server:
        response = await asyncio.to_thread(
            bridge.handle_message,
            {
                "type": "capture",
                "url": server.base + "/missing.bin",
                "filename": "video.mp4",
                "source": "extension",
            },
        )
    assert response["type"] == "capture_error"
    assert "404" in response["message"]
    await context.shutdown()


@pytest.mark.asyncio
async def test_capture_probe_accepts_real_file(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    with PayloadServer(b"x" * 4096) as server:
        response = await asyncio.to_thread(
            bridge.handle_message,
            {
                "type": "capture",
                "url": server.url,
                "filename": "video.mp4",
                "source": "extension",
            },
        )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].url == server.url
    await context.shutdown()


@pytest.mark.asyncio
async def test_capture_probe_allows_streaming_html(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    html = b"<html><head><title>Watch</title></head><body>player</body></html>"
    # Streaming pages legitimately answer an HTML shell to a bare GET; the
    # probe must not reject them (yt-dlp resolves the real media later).
    monkeypatch.setattr("magnetoclip.browser.manager.is_streaming_url", lambda url: True)
    with html_server(html) as server:
        response = await asyncio.to_thread(
            bridge.handle_message,
            {
                "type": "capture",
                "url": server.url,
                "filename": "video.mp4",
                "source": "extension",
            },
        )
    assert response["type"] == "capture_pending"
    await context.shutdown()


def test_settings_refresh_from_store(tmp_path):
    context = make_context(tmp_path)
    from magnetoclip.database.repositories import SettingsStore

    # The app persists toggles; the host process only keeps a startup snapshot.
    SettingsStore(context.session_factory).save("browser.default_downloader", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "settings"})
    assert response["type"] == "settings_ok"
    assert response["default_downloader"] is True
    response = bridge.handle_message({"type": "status"})
    assert response["default_downloader"] is True
    asyncio.run(context.shutdown())


def test_capture_pending_when_confirm_enabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "id": 7,
            "type": "capture",
            "url": "https://example.com/media.mp4",
            "filename": "media.mp4",
            "detected_type": "video",
            "source": "extension",
        }
    )
    assert response["type"] == "capture_pending"
    assert response["id"] == 7
    assert response["filename"] == "media.mp4"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].url == "https://example.com/media.mp4"
    asyncio.run(context.shutdown())


def test_extension_non_media_skipped_when_not_default_downloader(tmp_path):
    """Out-of-scope extension captures are refused so the browser keeps the file.

    The extension may offer a non-media browser download before its own
    settings snapshot has re-synced after a service-worker wake; the app must
    not queue files the user asked to leave to the browser.
    """
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "source": "extension",
        }
    )
    assert response["type"] == "capture_skipped"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        assert PendingCaptureRepository(session).pending() == []
    asyncio.run(context.shutdown())


def test_extension_non_media_skipped_when_capture_disabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.capture_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/video.mp4",
            "detected_type": "video",
            "source": "extension",
        }
    )
    assert response["type"] == "capture_skipped"
    asyncio.run(context.shutdown())


def test_extension_media_captured_when_not_default_downloader(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/stream?format=mp4",
            "filename": "stream.mp4",
            "detected_type": "video",
            "source": "extension",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].url == "https://example.com/stream?format=mp4"
    asyncio.run(context.shutdown())


def test_extension_non_media_accepted_when_default_downloader(tmp_path):
    """The default-downloader scope overrides the media-only gate."""
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    context.settings.set("browser.default_downloader", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "source": "extension",
        }
    )
    assert response["type"] == "capture_ok"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        assert PendingCaptureRepository(session).pending() == []
    asyncio.run(context.shutdown())


def test_context_menu_capture_always_pends(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "referrer": "https://example.com/",
            "source": "context_menu",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        assert len(PendingCaptureRepository(session).pending()) == 1
    asyncio.run(context.shutdown())


def test_context_menu_capture_pends_when_confirm_enabled(tmp_path):
    """Right-click captures open the confirmation dialog rather than starting
    the download automatically, so users can review the URL before downloading."""  # noqa: E501
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://upload.wikimedia.org/wikipedia/example.jpg",
            "filename": "example.jpg",
            "referrer": "https://en.wikipedia.org/example",
            "source": "context_menu",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].url == "https://upload.wikimedia.org/wikipedia/example.jpg"
    assert pending[0].referrer == "https://en.wikipedia.org/example"
    asyncio.run(context.shutdown())


def test_popup_capture_always_pends(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "source": "popup",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        assert len(PendingCaptureRepository(session).pending()) == 1
    asyncio.run(context.shutdown())


# ----- blob capture (Telegram-style in-memory media) -----


def test_capture_blob_with_data_base64_pending(tmp_path):
    import base64

    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    payload = b"\x89PNG\r\n\x1a\n" + bytes(512)
    encoded = base64.b64encode(payload).decode("ascii")
    # Data captures carry a blob: URL, so the HTTP probe is skipped entirely
    # and the response is synchronous.
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
            "filename": "",
            "mime_type": "image/png",
            "data_base64": encoded,
            "source": "page_scan",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].data_base64 == encoded
    assert pending[0].filename == "captured-media.png"
    asyncio.run(context.shutdown())


def test_capture_blob_rejected_without_data(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
            "filename": "x.png",
            "source": "context_menu",
        }
    )
    assert response["type"] == "capture_error"
    asyncio.run(context.shutdown())


@pytest.mark.asyncio
async def test_capture_blob_starts_completed_download(tmp_path):
    import base64

    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    payload = b"\x89PNG\r\n\x1a\n" + bytes(1024)
    encoded = base64.b64encode(payload).decode("ascii")
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
            "mime_type": "image/png",
            "data_base64": encoded,
            "source": "extension",
        }
    )
    assert response["type"] == "capture_ok"
    download = context.manager.get_download(response["download_id"])
    assert download.status == DownloadStatus.completed
    assert Path(download.save_path).read_bytes() == payload
    await context.shutdown()


# ----- chunked blob capture (large Telegram photos/videos) -----


def test_capture_chunk_assembles_and_pends(tmp_path):
    import base64

    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    payload = bytes(range(256)) * 8000  # ~2MB of non-trivial bytes
    encoded = base64.b64encode(payload).decode("ascii")
    # Split on base64 multiples of 4 so each chunk stays valid base64.
    chunk_size = (len(encoded) // 3 // 4) * 4
    chunks = [encoded[i * chunk_size : (i + 1) * chunk_size] for i in range(2)]
    chunks.append(encoded[2 * chunk_size :])
    assert "".join(chunks) == encoded

    response = None
    for index, chunk in enumerate(chunks):
        response = bridge.handle_message(
            {
                "type": "capture_chunk",
                "capture_key": "tg-photo-1",
                "index": index,
                "total": 3,
                "chunk": chunk,
                "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
                "filename": "",
                "mime_type": "image/png",
                "detected_type": "image",
                "referrer": "https://web.telegram.org/a/#123",
                "last": index == 2,
            }
        )
        if index < 2:
            assert response["type"] == "capture_chunk_ok"

    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].data_base64 == encoded
    assert pending[0].filename == "captured-media.png"
    assert pending[0].referrer == "https://web.telegram.org/a/#123"
    asyncio.run(context.shutdown())


def test_capture_chunk_assembles_out_of_order(tmp_path):
    import base64

    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    payload = bytes(1024)
    encoded = base64.b64encode(payload).decode("ascii")
    third = len(encoded) // 3

    responses = [
        bridge.handle_message(
            {
                "type": "capture_chunk",
                "capture_key": "tg-photo-2",
                "index": index,
                "total": 3,
                "chunk": encoded[index * third : (index + 1) * third],
                "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
                "last": index == 2,
            }
        )
        for index in (2, 0, 1)
    ]
    assert [r["type"] for r in responses] == [
        "capture_chunk_ok",
        "capture_chunk_ok",
        "capture_pending",
    ]
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].data_base64 == encoded
    asyncio.run(context.shutdown())


def test_capture_chunk_streaming_finalizes_on_last(tmp_path):
    import base64

    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    payload = bytes(range(256)) * 4000  # ~1MB
    encoded = base64.b64encode(payload).decode("ascii")
    # Splitting on base64 multiples of 4 keeps individual chunks decodable.
    chunk_size = (len(encoded) // 3 // 4) * 4
    chunks = [encoded[i * chunk_size : (i + 1) * chunk_size] for i in range(3)]
    chunks = [c for c in chunks if c]
    assert "".join(chunks) == encoded

    proxy = "https://web.telegram.org/a/progressive/document1?size=700000&quality=m"
    responses = []
    for index, chunk in enumerate(chunks):
        response = bridge.handle_message(
            {
                "type": "capture_chunk",
                "capture_key": "tg-live-1",
                "index": index,
                "total": 0,
                "chunk": chunk,
                "url": proxy,
                "filename": "document1.mp4",
                "mime_type": "video/mp4",
                "detected_type": "video",
                "referrer": "https://web.telegram.org/a/#123",
                "last": False,
            }
        )
        responses.append(response["type"])
    final = bridge.handle_message(
        {
            "type": "capture_chunk",
            "capture_key": "tg-live-1",
            "index": len(chunks),
            "total": 0,
            "chunk": "",
            "url": proxy,
            "filename": "document1.mp4",
            "mime_type": "video/mp4",
            "detected_type": "video",
            "referrer": "https://web.telegram.org/a/#123",
            "last": True,
        }
    )
    assert responses == ["capture_chunk_ok"] * len(chunks)
    assert final["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].data_base64 == encoded
    assert pending[0].filename == "document1.mp4"
    assert pending[0].referrer == "https://web.telegram.org/a/#123"
    asyncio.run(context.shutdown())


def test_capture_chunk_stream_rejects_out_of_order(tmp_path):
    import base64

    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture_chunk",
            "capture_key": "tg-live-2",
            "index": 1,
            "total": 0,
            "chunk": base64.b64encode(b"first-bytes").decode("ascii"),
            "url": "https://web.telegram.org/a/progressive/document2",
            "filename": "document2.mp4",
            "mime_type": "video/mp4",
            "detected_type": "video",
            "last": False,
        }
    )
    assert response["type"] == "capture_chunk_error"
    assert "out of order" in response["message"]
    asyncio.run(context.shutdown())


def test_capture_chunk_stream_empty_rejected(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture_chunk",
            "capture_key": "tg-live-3",
            "index": 0,
            "total": 0,
            "chunk": "",
            "url": "https://web.telegram.org/a/progressive/document3",
            "filename": "document3.mp4",
            "mime_type": "video/mp4",
            "detected_type": "video",
            "last": True,
        }
    )
    assert response["type"] == "capture_chunk_error"
    assert "empty" in response["message"]
    asyncio.run(context.shutdown())


def test_capture_chunk_incomplete_expires(tmp_path):
    import base64

    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge._CHUNK_EXPIRY_SECONDS = -1
    bridge.handle_message(
        {
            "type": "capture_chunk",
            "capture_key": "tg-photo-3",
            "index": 0,
            "total": 2,
            "chunk": base64.b64encode(b"half").decode("ascii"),
            "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
            "last": False,
        }
    )
    assert "tg-photo-3" in bridge._chunk_assemblies
    bridge.handle_message(
        {
            "type": "capture_chunk",
            "capture_key": "tg-photo-4",
            "index": 0,
            "total": 1,
            "chunk": base64.b64encode(b"data").decode("ascii"),
            "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
            "last": True,
        }
    )
    assert "tg-photo-3" not in bridge._chunk_assemblies
    asyncio.run(context.shutdown())


def test_capture_chunk_rejects_corrupt_base64(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture_chunk",
            "capture_key": "tg-photo-4",
            "index": 0,
            "total": 1,
            "chunk": "!!!not-base64!!!",
            "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
            "last": True,
        }
    )
    assert response["type"] == "capture_chunk_error"
    assert "corrupt" in response["message"]
    asyncio.run(context.shutdown())


@pytest.mark.asyncio
async def test_capture_chunk_starts_completed_download(tmp_path):
    import base64

    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    payload = b"\x89PNG\r\n\x1a\n" + bytes(4096)
    encoded = base64.b64encode(payload).decode("ascii")

    response = None
    encoded = base64.b64encode(payload).decode("ascii")
    half = len(encoded) // 2
    chunks = [encoded[:half], encoded[half:]]
    for index, chunk in enumerate(chunks):
        response = bridge.handle_message(
            {
                "type": "capture_chunk",
                "capture_key": "tg-video-1",
                "index": index,
                "total": 2,
                "chunk": chunk,
                "url": "blob:https://web.telegram.org/55d2a84a-91c1-4b2e-8a13-0f0e0c2e8f1c",
                "mime_type": "image/png",
                "detected_type": "image",
                "source": "extension",
                "last": index == 1,
            }
        )
    assert response["type"] == "capture_ok"
    download = context.manager.get_download(response["download_id"])
    assert download.status == DownloadStatus.completed
    assert Path(download.save_path).read_bytes() == payload
    await context.shutdown()


def test_capture_skipped_while_skip_all_active(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    enable_skip_all(context, duration=None)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "source": "page_scan",
        }
    )
    assert response["type"] == "capture_skipped"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        repo = PendingCaptureRepository(session)
        assert repo.pending() == []
        assert repo.get(response["id"]).status == "rejected"
    asyncio.run(context.shutdown())


def test_capture_pending_after_skip_all_window_expires(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    context.settings.set(
        "browser.skip_all_until",
        (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "source": "page_scan",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].status == "pending"
    asyncio.run(context.shutdown())


def test_skip_all_reads_live_db_not_stale_snapshot(tmp_path):
    context = make_context(tmp_path)
    from magnetoclip.database.repositories import SettingsStore

    # Regression: unchecking "Skip all" in the app clears the settings table,
    # but the native-messaging host runs as a separate process with its own
    # in-memory snapshot. skip_all_active must consult the DB so the host stops
    # auto-rejecting captures immediately, without waiting for a refresh.
    context.settings.set(
        "browser.skip_all_until",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    SettingsStore(context.session_factory).save("browser.skip_all_until", "")

    assert skip_all_active(context) is False
    asyncio.run(context.shutdown())


def test_capture_pending_stores_cookies(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/media.mp4",
            "filename": "media.mp4",
            "detected_type": "video",
            "source": "extension",
            "cookies": "SID=abc123; HSID=def456",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].cookies_json == {"SID": "abc123", "HSID": "def456"}
    asyncio.run(context.shutdown())


def test_capture_pending_accepts_cookie_dict(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "cookies": {"SID": "abc123"},
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert pending[0].cookies_json == {"SID": "abc123"}
    asyncio.run(context.shutdown())


@pytest.mark.asyncio
async def test_capture_immediate_start_passes_cookies(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())

    with PayloadServer(b"z" * 2048) as server:
        response = bridge.handle_message(
            {
                "type": "capture",
                "url": server.url,
                "filename": "captured.bin",
                "cookies": "SID=abc123; HSID=def456",
            }
        )
        assert response["type"] == "capture_ok"
        download = context.manager.get_download(response["download_id"])
        headers = dict(download.headers_json or {})
        assert headers.get("cookie") == "SID=abc123; HSID=def456"
        deadline = asyncio.get_running_loop().time() + 15
        status = None
        while asyncio.get_running_loop().time() < deadline:
            status = context.manager.get_download(response["download_id"]).status.value
            if status in ("completed", "failed", "verification_failed"):
                break
            await asyncio.sleep(0.02)
        assert status == "completed", f"expected completed, got {status}"
    await context.shutdown()


def test_page_scan_capture_always_pending(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://x.com/user/status/123/video",
            "filename": "video.mp4",
            "referrer": "https://x.com/user/status/123",
            "source": "page_scan",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].source == "page_scan"
    asyncio.run(context.shutdown())


def test_capture_dedupes_pending(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    message = {"type": "capture", "url": "https://example.com/file.zip"}
    first = bridge.handle_message(message)
    second = bridge.handle_message(message)
    assert first["type"] == "capture_pending"
    assert second["type"] == "capture_pending"
    assert first["id"] == second["id"]
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        assert len(PendingCaptureRepository(session).pending()) == 1
    asyncio.run(context.shutdown())


def test_page_scan_records_detection(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    posted: list[dict] = []
    context.events.connect(Events.BROWSER_EVENT, posted.append)
    response = bridge.handle_message(
        {
            "type": "page_scan",
            "url": "https://example.com/page",
            "files": [
                {"url": "https://example.com/a.zip", "filename": "a.zip", "detected_type": "archive"},
                {"url": "https://example.com/b.pdf", "filename": "b.pdf", "detected_type": "document"},
            ],
        }
    )
    assert response["type"] == "page_scan_ok"
    assert response["count"] == 2
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserDetectionRepository

        detections = BrowserDetectionRepository(session).unnotified()
    assert len(detections) == 1
    assert detections[0].page_url == "https://example.com/page"
    assert detections[0].count == 2
    assert posted and posted[0]["source"] == "page_scan"
    asyncio.run(context.shutdown())


def test_page_scan_rejected_when_integration_disabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {"type": "page_scan", "url": "https://example.com/page", "files": []}
    )
    assert response["type"] == "page_scan_error"
    asyncio.run(context.shutdown())


def test_request_id_is_echoed(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"id": 42, "type": "ping"})
    assert response == {"id": 42, "type": "pong"}
    asyncio.run(context.shutdown())


@pytest.mark.asyncio
async def test_capture_rejects_bad_urls(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    for bad in ("", "not a url", "ftp://example.com/x", "file:///etc/passwd"):
        response = bridge.handle_message({"type": "capture", "url": bad})
        assert response["type"] == "capture_error", bad
    await context.shutdown()


@pytest.mark.asyncio
async def test_status_reports_counts(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    response = bridge.handle_message({"type": "status"})
    assert response["type"] == "status_ok"
    assert response["active"] == 0
    assert response["completed"] == 0
    assert response["integration_enabled"] is True
    assert response["default_downloader"] is False

    with PayloadServer(b"y" * 2048) as server:
        download = context.manager.add(server.url, filename="one.bin")
        context.manager.start(download.id)
        await asyncio.sleep(0.1)
        response = bridge.handle_message({"type": "status"})
        assert response["active"] == 1 or response["completed"] == 1
    await context.shutdown()


# ----- installer -----


def test_extension_id_stable_and_valid(tmp_path):
    key_path, public_key = ensure_extension_key(tmp_path)
    assert key_path.exists()
    first = extension_id_from_public_key(public_key)
    second = extension_id_from_public_key(public_key)
    assert first == second
    assert len(first) == 32
    assert all(c in "abcdefghijklmnop" for c in first)


def test_extension_id_matches_chromium_algorithm(tmp_path):
    import hashlib

    from cryptography.hazmat.primitives import serialization

    from magnetoclip.browser.integration.install import extension_id_from_public_key

    _key_path, public_key = ensure_extension_key(tmp_path)
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    alphabet = "abcdefghijklmnop"
    expected = ""
    for byte in hashlib.sha256(spki).digest()[:16]:
        expected += alphabet[byte >> 4]
        expected += alphabet[byte & 0x0F]
    assert extension_id_from_public_key(public_key) == expected


def test_host_manifest_chromium():
    import json as jsonlib

    from magnetoclip.browser.integration.install import build_host_manifest

    manifest = build_host_manifest(
        host_path="C:/app/magnetoclip.exe", extension_id="a" * 32, browser="chrome", data_dir=None
    )
    assert manifest["name"] == "com.magnetoclip.host"
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == ["chrome-extension://" + "a" * 32 + "/"]
    assert jsonlib.dumps(manifest)


def test_host_manifest_firefox():
    manifest = build_host_manifest(
        host_path="C:/app/magnetoclip.exe", extension_id="a" * 32, browser="firefox", data_dir=None
    )
    assert manifest["allowed_extensions"] == ["magneto-companion@magnetoclip.app"]
    assert "allowed_origins" not in manifest


def test_host_manifest_path_per_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    chrome = host_manifest_path("chrome", tmp_path)
    assert str(chrome).endswith(
        "Local\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host.json"
    )
    firefox = host_manifest_path("firefox", tmp_path)
    assert str(firefox).endswith(
        "Roaming\\Mozilla\\NativeMessagingHosts\\com.magnetoclip.host.json"
    )


def test_native_hosts_registry_key_per_browser():
    import importlib

    install_mod = importlib.import_module("magnetoclip.browser.integration.install")

    assert (
        install_mod._native_hosts_registry_key("chrome")
        == "Software\\Google\\Chrome\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("edge")
        == "Software\\Microsoft\\Edge\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("chromium")
        == "Software\\Chromium\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("brave")
        == "Software\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("vivaldi")
        == "Software\\Vivaldi\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("firefox")
        == "Software\\Mozilla\\NativeMessagingHosts"
    )


def test_register_and_unregister_native_host(monkeypatch, tmp_path):
    import importlib

    install_mod = importlib.import_module("magnetoclip.browser.integration.install")

    created: list[tuple[str, str]] = []
    values: list[tuple[str, str, str, str]] = []
    deleted: list[tuple[str, str]] = []

    class FakeKey:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeWinreg:
        HKEY_CURRENT_USER = "HKCU"
        REG_SZ = "REG_SZ"
        KEY_SET_VALUE = 0x0002
        KEY_WOW64_64KEY = 0x0100

        @staticmethod
        def CreateKey(hive, key):
            created.append((hive, key))
            return FakeKey(key)

        @staticmethod
        def CreateKeyEx(hive, key, reserved, access):
            created.append((hive, key))
            return FakeKey(key)

        @staticmethod
        def SetValue(key, name, value_type, value):
            values.append((key.name, name, value_type, value))

        @staticmethod
        def OpenKey(hive, key, reserved, access):
            return FakeKey(key)

        @staticmethod
        def DeleteKey(key, subkey):
            deleted.append((key.name, subkey))

    monkeypatch.setattr(install_mod, "winreg", FakeWinreg)

    manifest = tmp_path / "com.magnetoclip.host.json"
    install_mod._register_native_host("chrome", manifest)
    assert created == [
        ("HKCU", "Software\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host")
    ]
    assert values == [
        (
            "Software\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host",
            "",
            "REG_SZ",
            str(manifest),
        )
    ]

    install_mod._register_native_host("firefox", manifest)
    assert created == [
        ("HKCU", "Software\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host"),
        ("HKCU", "Software\\Mozilla\\NativeMessagingHosts\\com.magnetoclip.host"),
    ]
    assert values == [
        (
            "Software\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host",
            "",
            "REG_SZ",
            str(manifest),
        ),
        (
            "Software\\Mozilla\\NativeMessagingHosts\\com.magnetoclip.host",
            "",
            "REG_SZ",
            str(manifest),
        ),
    ]

    install_mod._unregister_native_host("chrome")
    assert deleted == [
        ("Software\\Google\\Chrome\\NativeMessagingHosts", "com.magnetoclip.host")
    ]
    install_mod._unregister_native_host("firefox")
    assert deleted == [
        ("Software\\Google\\Chrome\\NativeMessagingHosts", "com.magnetoclip.host"),
        ("Software\\Mozilla\\NativeMessagingHosts", "com.magnetoclip.host"),
    ]


def test_registry_noop_when_winreg_unavailable(monkeypatch, tmp_path):
    import importlib

    install_mod = importlib.import_module("magnetoclip.browser.integration.install")

    monkeypatch.setattr(install_mod, "winreg", None)
    install_mod._register_native_host("chrome", tmp_path / "x.json")
    install_mod._unregister_native_host("chrome")


# ----- app->extension requests (blob: URL fetches) -----


def make_blob_request(context, url="blob:https://web.telegram.org/abc"):
    from magnetoclip.database.repositories import BrowserRequestRepository

    with context.session_factory() as session:
        return BrowserRequestRepository(session).add(
            "fetch_blob", payload={"url": url}
        )


def test_next_outbound_message_claims_queued_request(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    request = make_blob_request(context)

    outbound = bridge.next_outbound_message()
    assert outbound == {
        "type": "fetch_blob",
        "request_id": request.id,
        "url": "blob:https://web.telegram.org/abc",
    }
    # A request can only be claimed once.
    assert bridge.next_outbound_message() is None
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "sent"
    asyncio.run(context.shutdown())


def test_next_outbound_message_background_fetch_passes_through(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    from magnetoclip.database.repositories import BrowserRequestRepository

    with context.session_factory() as session:
        request = BrowserRequestRepository(session).add(
            "fetch_blob",
            payload={
                "url": "https://cdn.imgnex.top/hls/master.m3u8",
                "background": True,
                "referrer": "https://anikai.watch/watch/episode-1",
            },
        )

    outbound = bridge.next_outbound_message()
    assert outbound == {
        "type": "fetch_blob",
        "request_id": request.id,
        "url": "https://cdn.imgnex.top/hls/master.m3u8",
        "background": True,
        "referrer": "https://anikai.watch/watch/episode-1",
    }
    asyncio.run(context.shutdown())


def test_next_outbound_message_empty_and_disabled(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    assert bridge.next_outbound_message() is None

    make_blob_request(context)
    context.settings.set("browser.integration_enabled", False)
    assert bridge.next_outbound_message() is None
    asyncio.run(context.shutdown())


def test_next_outbound_message_unsupported_type_marked_error(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    from magnetoclip.database.repositories import BrowserRequestRepository

    # A claimed type that cannot be turned into a message must be recorded as an
    # error rather than silently dropped.
    with context.session_factory() as session:
        request = BrowserRequestRepository(session).add("fetch_blob", payload={})
    assert bridge.next_outbound_message() is None
    with context.session_factory() as session:
        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "error"
    asyncio.run(context.shutdown())


def test_next_outbound_message_leaves_request_types_it_does_not_own(tmp_path):
    """The writer and the UI poll the same table for different purposes."""
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    from magnetoclip.database.repositories import BrowserRequestRepository

    with context.session_factory() as session:
        request = BrowserRequestRepository(session).add(
            "navigate", payload={"page": "detected", "category": ""}
        )
    assert bridge.next_outbound_message() is None
    with context.session_factory() as session:
        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "queued", "the UI must still be able to claim it"
    asyncio.run(context.shutdown())


def test_blob_fetch_chunk_reassembles_out_of_order(tmp_path):
    import base64

    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    request = make_blob_request(context)
    payload = b"video/mp4-0123456789"
    encoded = base64.b64encode(payload).decode("ascii")
    chunks = [encoded[:4], encoded[4:8], encoded[8:]]

    # Out-of-order arrival to exercise index-based assembly.
    for index in (2, 0, 1):
        response = bridge.handle_message(
            {
                "type": "blob_fetch_chunk",
                "request_id": request.id,
                "index": index,
                "total": 3,
                "chunk": chunks[index],
                "url": "blob:https://web.telegram.org/abc",
                "mime_type": "video/mp4",
                "filename": "clip.mp4",
            }
        )
        assert response["type"] == "blob_fetch_chunk_ok"

    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "ready"
    assert base64.b64decode(loaded.data_base64) == payload
    assert loaded.result_json["mime_type"] == "video/mp4"
    assert loaded.result_json["filename"] == "clip.mp4"
    asyncio.run(context.shutdown())


def test_blob_fetch_chunk_partial_keeps_request_open(tmp_path):
    import base64

    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    request = make_blob_request(context)
    bridge.next_outbound_message()  # host claims it (status -> sent)
    encoded = base64.b64encode(b"x" * 32).decode("ascii")
    half = len(encoded) // 2

    bridge.handle_message(
        {
            "type": "blob_fetch_chunk",
            "request_id": request.id,
            "index": 0,
            "total": 2,
            "chunk": encoded[:half],
        }
    )
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "sent"
    assert loaded.data_base64 is None
    asyncio.run(context.shutdown())


def test_blob_fetch_chunk_corrupt_marks_error(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    request = make_blob_request(context)

    response = bridge.handle_message(
        {
            "type": "blob_fetch_chunk",
            "request_id": request.id,
            "index": 0,
            "total": 1,
            "chunk": "!!!not-base64!!!",
        }
    )
    assert response["type"] == "blob_fetch_error"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "error"
    assert "corrupt" in loaded.result_json["message"]
    asyncio.run(context.shutdown())


def test_blob_fetch_result_error_marks_request(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    request = make_blob_request(context)

    response = bridge.handle_message(
        {"type": "blob_fetch_result", "request_id": request.id, "error": "tab closed"}
    )
    assert response["type"] == "blob_fetch_result_ok"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "error"
    assert loaded.result_json["message"] == "tab closed"
    asyncio.run(context.shutdown())


def test_blob_fetch_result_resolved_url_marks_ready(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    request = make_blob_request(context)

    response = bridge.handle_message(
        {
            "type": "blob_fetch_result",
            "request_id": request.id,
            "resolved_url": "https://cdn.telegram.org/file/abc/clip.mp4",
            "cookies": "stel=1",
            "referrer": "https://web.telegram.org/a/",
            "mime_type": "video/mp4",
        }
    )
    assert response["type"] == "blob_fetch_result_ok"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserRequestRepository

        loaded = BrowserRequestRepository(session).get(request.id)
    assert loaded.status == "ready"
    assert loaded.data_base64 is None
    assert loaded.result_json["resolved_url"] == "https://cdn.telegram.org/file/abc/clip.mp4"
    assert loaded.result_json["cookies"] == "stel=1"
    assert loaded.result_json["referrer"] == "https://web.telegram.org/a/"
    asyncio.run(context.shutdown())
