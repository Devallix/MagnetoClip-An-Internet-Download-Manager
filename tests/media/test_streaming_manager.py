"""Manager integration for the streaming (yt-dlp) download path."""

from __future__ import annotations

import asyncio

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.media.streaming import DownloadCancelled, StreamInfo


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    context.settings.set("streaming.quality", "best")
    return context


def fake_info(**overrides) -> StreamInfo:
    base = {
        "url": "https://www.youtube.com/watch?v=abc",
        "title": "Demo Video",
        "ext": "mp4",
        "media_type": "video",
        "size": 1_000_000,
        "duration": 60.0,
        "webpage_url": None,
        "formats": [],
    }
    base.update(overrides)
    return StreamInfo(**base)


async def wait_for_status(manager, download_id, status, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        d = manager.get_download(download_id)
        if d is not None and d.status.value == status:
            return d
        await asyncio.sleep(0.02)
    raise AssertionError(f"status never reached {status}")


def test_add_routes_streaming_to_videos_category(tmp_path):
    context = make_context(tmp_path)
    download = context.manager.add(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", filename="ignored.mp4"
    )
    assert download.detected_type == "video"
    category = context.categories.get(download.category_id)
    assert category is not None and category.name == "Videos"
    context.database.close()


def test_add_audio_platform_routes_to_music(tmp_path):
    context = make_context(tmp_path)
    download = context.manager.add("https://soundcloud.com/user/track")
    assert download.detected_type == "audio"
    category = context.categories.get(download.category_id)
    assert category is not None and category.name == "Music"
    context.database.close()


@pytest.mark.asyncio
async def test_streaming_completion_rewrites_filename(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: fake_info(title="Real Title"),
    )

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None):
        save_dir = save_dir / "Real Title.mp4"
        save_dir.write_bytes(b"x" * 1234)
        return save_dir

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream",
        fake_download_stream,
    )

    download = context.manager.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    final = context.manager.get_download(download.id)
    assert final.filename == "Real Title.mp4"
    assert final.save_path.endswith("Real Title.mp4")
    assert final.size_total == 1234
    assert final.detected_type == "video"
    await context.shutdown()


@pytest.mark.asyncio
async def test_streaming_failure_marks_failed(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: (_ for _ in ()).throw(
            __import__("magnetoclip.media.streaming", fromlist=["StreamResolutionError"]).StreamResolutionError("nope")
        ),
    )

    download = context.manager.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "failed")
    final = context.manager.get_download(download.id)
    assert "resolution failed" in (final.error or "")
    await context.shutdown()


@pytest.mark.asyncio
async def test_streaming_cancel_marks_stopped(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: fake_info(),
    )
    cancel_event_holder = {}

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None):
        cancel_event_holder["event"] = cancel_event
        progress_cb(
            {
                "status": "downloading",
                "downloaded_bytes": 100,
                "total_bytes": 1000,
            }
        )
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise DownloadCancelled("cancelled")

    import time

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream",
        fake_download_stream,
    )

    download = context.manager.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    context.manager.start(download.id)
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline and "event" not in cancel_event_holder:
        await asyncio.sleep(0.02)

    context.manager.cancel(download.id)
    await wait_for_status(context.manager, download.id, "stopped")
    assert cancel_event_holder["event"].is_set()
    await context.shutdown()


@pytest.mark.asyncio
async def test_streaming_pause_resume_restarts(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: fake_info(),
    )
    calls = []

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None):
        calls.append("start")
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise DownloadCancelled("cancelled")

    import time

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream",
        fake_download_stream,
    )

    download = context.manager.add("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    context.manager.start(download.id)
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline and not calls:
        await asyncio.sleep(0.02)

    context.manager.pause(download.id)
    await wait_for_status(context.manager, download.id, "paused")

    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline and download.id in context.manager._tasks:
        await asyncio.sleep(0.02)

    context.manager.resume(download.id)
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline and len(calls) < 2:
        await asyncio.sleep(0.02)
    assert len(calls) == 2
    context.manager.cancel(download.id)
    await context.shutdown()


@pytest.mark.asyncio
async def test_streaming_cookies_reach_resolve_and_download(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    seen = {}

    def fake_resolve(url, quality, **kwargs):
        seen["resolve_cookies"] = kwargs.get("cookies")
        return fake_info(title="Cookie Video")

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None):
        seen["download_cookies"] = cookies
        path = save_dir / "Cookie Video.mp4"
        path.write_bytes(b"x" * 4321)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream", fake_resolve
    )
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add(
        "https://www.youtube.com/watch?v=cookied",
        cookies={"SID": "abc123", "HSID": "def456"},
    )
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert seen["resolve_cookies"] == {"SID": "abc123", "HSID": "def456"}
    assert seen["download_cookies"] == {"SID": "abc123", "HSID": "def456"}
    await context.shutdown()


def test_stream_cookies_parses_stored_header(tmp_path):
    context = make_context(tmp_path)
    download = context.manager.add(
        "https://www.youtube.com/watch?v=cookie2",
        cookies={"SID": "abc123", "HSID": "def456"},
    )
    parsed = context.manager._stream_cookies(download)
    assert parsed == {"SID": "abc123", "HSID": "def456"}
    context.database.close()


def test_stream_cookies_none_without_header(tmp_path):
    context = make_context(tmp_path)
    download = context.manager.add("https://www.youtube.com/watch?v=plain")
    assert context.manager._stream_cookies(download) is None
    context.database.close()
