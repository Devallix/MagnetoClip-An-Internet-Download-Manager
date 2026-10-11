"""Manager integration for the streaming (yt-dlp) download path."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.core.downloads.manager import DownloadManager
from magnetoclip.media.hls.sniff import SniffResult
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

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        target = save_dir / (filename or "Real Title.mp4")
        target.write_bytes(b"x" * 1234)
        return target

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

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
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

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
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
async def test_streaming_resume_reuses_partial(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    resolve_calls = []
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: (
            resolve_calls.append(url),
            fake_info(title="New Title"),
        )[1],
    )
    seen = {}

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        seen["filename"] = filename
        target = save_dir / filename
        target.write_bytes(b"x" * 2048)
        return target

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream",
        fake_download_stream,
    )

    download = context.manager.add("https://www.youtube.com/watch?v=resume")
    target = Path(context.settings.get("downloads.default_directory")) / "Demo Video.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(str(target) + ".part").write_bytes(b"x" * 512)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import DownloadRepository

        row = DownloadRepository(session).get(download.id)
        row.save_path = str(target)
        row.filename = "Demo Video.mp4"
        session.commit()

    context.manager.start(download.id)
    await wait_for_status(context.manager, download.id, "completed")
    assert resolve_calls == []
    assert seen["filename"] == "Demo Video.mp4"
    final = context.manager.get_download(download.id)
    assert final.save_path.endswith("Demo Video.mp4")
    await context.shutdown()


@pytest.mark.asyncio
async def test_streaming_resume_reuses_merged_format_parts(tmp_path, monkeypatch):
    """yt-dlp DASH/merged downloads leave ``name.fXXX.part`` intermediates and
    no plain ``name.part``; a paused download must detect them as partial data
    and continue from the stored filename instead of re-resolving the stream."""
    context = make_context(tmp_path)
    resolve_calls = []
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: (
            resolve_calls.append(url),
            fake_info(title="New Title"),
        )[1],
    )
    seen = {}

    def fake_download_stream(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        seen["filename"] = filename
        target = save_dir / filename
        target.write_bytes(b"x" * 4096)
        return target

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream",
        fake_download_stream,
    )

    download = context.manager.add("https://www.youtube.com/watch?v=merged")
    target = Path(context.settings.get("downloads.default_directory")) / "Demo Video.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(str(target) + ".f137.mp4.part").write_bytes(b"x" * 2048)
    Path(str(target) + ".f140.m4a.part").write_bytes(b"x" * 2048)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import DownloadRepository

        row = DownloadRepository(session).get(download.id)
        row.save_path = str(target)
        row.filename = "Demo Video.mp4"
        session.commit()

    context.manager.start(download.id)
    await wait_for_status(context.manager, download.id, "completed")
    assert resolve_calls == []
    assert seen["filename"] == "Demo Video.mp4"
    final = context.manager.get_download(download.id)
    assert final.save_path.endswith("Demo Video.mp4")
    await context.shutdown()


def test_delete_parts_removes_all_part_files(tmp_path):
    context = make_context(tmp_path)
    download = context.manager.add("https://example.com/file.bin", filename="file.bin")
    target = Path(context.settings.get("downloads.default_directory")) / "file.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    with context.session_factory() as session:
        from magnetoclip.database.repositories import DownloadRepository

        row = DownloadRepository(session).get(download.id)
        row.save_path = str(target)
        session.commit()
    target.write_bytes(b"")
    for suffix in (".mclip", ".part", ".part0", ".part9", ".part10", ".part11", ".part-frag1.ytdl"):
        Path(f"{target}{suffix}").write_bytes(b"x")

    context.manager._delete_parts(download.id)

    for suffix in (".mclip", ".part", ".part0", ".part9", ".part10", ".part11", ".part-frag1.ytdl"):
        assert not Path(f"{target}{suffix}").exists(), suffix
    assert target.exists()
    context.database.close()


@pytest.mark.asyncio
async def test_streaming_cookies_and_headers_reach_resolve_and_download(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    seen = {}

    def fake_resolve(url, quality, **kwargs):
        seen["resolve_cookies"] = kwargs.get("cookies")
        seen["resolve_headers"] = kwargs.get("headers")
        return fake_info(title="Cookie Video")

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        seen["download_cookies"] = cookies
        seen["download_headers"] = headers
        path = save_dir / (filename or "Cookie Video.mp4")
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
        headers={"Referer": "https://player.example/embed?sid=x"},
    )
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert seen["resolve_cookies"] == {"SID": "abc123", "HSID": "def456"}
    assert seen["download_cookies"] == {"SID": "abc123", "HSID": "def456"}
    assert seen["resolve_headers"] == {"Referer": "https://player.example/embed?sid=x"}
    assert seen["download_headers"] == {"Referer": "https://player.example/embed?sid=x"}
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


@pytest.mark.asyncio
async def test_m3u8_falls_back_to_browser_on_bot_wall(tmp_path, monkeypatch):
    """When yt-dlp hits a Cloudflare bot-wall on an m3u8 URL and browser_assist
    is enabled, the manager should fall back to the browser-assisted path."""
    context = make_context(tmp_path)
    context.settings.set("streaming.browser_assist", True)
    seen = {}

    def fake_browser_download(
        url,
        save_dir,
        quality,
        *,
        session_factory,
        referer=None,
        basename=None,
        progress_cb=None,
        cancel_event=None,
        fetch_timeout=90.0,
        poll_interval=0.25,
        ffmpeg=None,
    ):
        seen["url"] = url
        seen["referer"] = referer
        target = save_dir / "episode.ts"
        target.write_bytes(b"browser bytes")
        return target

    from magnetoclip.media.streaming import StreamResolutionError

    def failing_resolve(*a, **k):
        raise StreamResolutionError("ERROR: [generic] master: Unable to download webpage: HTTP Error 403: Forbidden")

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        failing_resolve,
    )
    monkeypatch.setattr(
        "magnetoclip.media.browser_streaming.download_browser_hls",
        fake_browser_download,
    )

    download = context.manager.add(
        "https://cdn.example/hls/master.m3u8",
        headers={"Referer": "https://anikai.watch/watch/episode-1"},
    )
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert seen["url"].endswith("master.m3u8")
    assert seen["referer"] == "https://anikai.watch/watch/episode-1"
    final = context.manager.get_download(download.id)
    assert final.filename == "episode.ts"
    assert final.size_total == len(b"browser bytes")
    await context.shutdown()


@pytest.mark.asyncio
async def test_m3u8_falls_back_to_ytdl_when_browser_unavailable(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("streaming.browser_assist", True)

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: fake_info(title="Ytdl Title"),
    )

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        target = save_dir / (filename or "Ytdl Title.mp4")
        target.write_bytes(b"ytdl bytes")
        return target

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    from magnetoclip.media import browser_streaming

    def failing_browser(*args, **kwargs):
        raise browser_streaming.BrowserUnavailableError("extension not connected")

    monkeypatch.setattr(
        "magnetoclip.media.browser_streaming.download_browser_hls", failing_browser
    )

    download = context.manager.add("https://cdn.example/hls/master.m3u8")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    final = context.manager.get_download(download.id)
    assert final.filename == "Ytdl Title.mp4"
    assert final.size_total == len(b"ytdl bytes")
    await context.shutdown()


@pytest.mark.asyncio
async def test_m3u8_falls_back_to_browser_on_non_bot_wall_error(tmp_path, monkeypatch):
    """Cloudflare's Managed Challenge often answers HTTP 200 + an HTML page,
    so yt-dlp reports a non-bot-wall error (e.g. 'Unsupported URL'). Any m3u8
    resolution failure with browser_assist enabled must still fall back to the
    browser-assisted path."""
    context = make_context(tmp_path)
    context.settings.set("streaming.browser_assist", True)
    browser_called = {"called": False, "url": None}

    def fake_browser_download(
        url,
        save_dir,
        quality,
        *,
        session_factory,
        referer=None,
        basename=None,
        progress_cb=None,
        cancel_event=None,
        fetch_timeout=90.0,
        poll_interval=0.25,
        ffmpeg=None,
    ):
        browser_called["called"] = True
        browser_called["url"] = url
        target = save_dir / "episode.ts"
        target.write_bytes(b"browser bytes")
        return target

    from magnetoclip.media.streaming import StreamResolutionError

    def failing_resolve(*a, **k):
        raise StreamResolutionError("Unsupported URL: https://example.com/video.m3u8")

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        failing_resolve,
    )
    monkeypatch.setattr(
        "magnetoclip.media.browser_streaming.download_browser_hls",
        fake_browser_download,
    )

    download = context.manager.add("https://example.com/video.m3u8")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert browser_called["called"]
    assert browser_called["url"] == "https://example.com/video.m3u8"
    await context.shutdown()


@pytest.mark.asyncio
async def test_m3u8_no_browser_fallback_when_disabled(tmp_path, monkeypatch):
    """When browser_assist is disabled, a plain m3u8 failure must not reach the
    browser path -- the download should simply fail."""
    context = make_context(tmp_path)
    context.settings.set("streaming.browser_assist", False)
    browser_called = {"called": False}

    def fake_browser_download(*args, **kwargs):
        browser_called["called"] = True
        raise RuntimeError("should not be called")

    from magnetoclip.media.streaming import StreamResolutionError

    def failing_resolve(*a, **k):
        raise StreamResolutionError("Unsupported URL: https://example.com/video.m3u8")

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        failing_resolve,
    )
    monkeypatch.setattr(
        "magnetoclip.media.browser_streaming.download_browser_hls",
        fake_browser_download,
    )

    download = context.manager.add("https://example.com/video.m3u8")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "failed")
    assert not browser_called["called"]
    final = context.manager.get_download(download.id)
    assert "resolution failed" in (final.error or "")
    await context.shutdown()


@pytest.mark.asyncio
async def test_non_hls_page_does_not_trigger_browser_fallback(tmp_path, monkeypatch):
    """A streaming-site page URL (not an m3u8 manifest) that yt-dlp cannot
    resolve with a non-bot-wall error must not route into the browser path --
    only manifest URLs (or bot-wall failures) do."""
    context = make_context(tmp_path)
    context.settings.set("streaming.browser_assist", True)
    context.settings.set("streaming.page_sniff", False)
    browser_called = {"called": False}

    def fake_browser_download(*args, **kwargs):
        browser_called["called"] = True
        raise RuntimeError("should not be called")

    from magnetoclip.media.streaming import StreamResolutionError

    def failing_resolve(*a, **k):
        raise StreamResolutionError("Unsupported URL: https://anikai.watch/watch/123")

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        failing_resolve,
    )
    monkeypatch.setattr(
        "magnetoclip.media.browser_streaming.download_browser_hls",
        fake_browser_download,
    )

    download = context.manager.add("https://anikai.watch/watch/123")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "failed")
    assert not browser_called["called"]
    final = context.manager.get_download(download.id)
    assert "resolution failed" in (final.error or "")
    await context.shutdown()


@pytest.mark.asyncio
async def test_page_sniff_feeds_manifest_to_stream(tmp_path, monkeypatch):
    """A streaming-site page URL is sniffed in-process; the resulting manifest
    URL and page Referer reach yt-dlp's resolve and download steps."""
    context = make_context(tmp_path)
    seen = {}

    async def fake_sniff(self, url, headers):
        seen["sniff_url"] = url
        return SniffResult(
            manifest_url="https://cdn.example/hls/master.m3u8?token=x",
            page_url=url,
            referer=url,
        )

    monkeypatch.setattr(DownloadManager, "_sniff_stream_page", fake_sniff)

    def fake_resolve(url, quality, **kwargs):
        seen["resolve_url"] = url
        seen["resolve_headers"] = kwargs.get("headers")
        return fake_info(title="Sniffed Ep")

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        seen["download_url"] = url
        seen["download_headers"] = headers
        path = save_dir / (filename or "Sniffed Ep.mp4")
        path.write_bytes(b"x" * 1234)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream", fake_resolve
    )
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add("https://hdtoday.to/watch/episode-9")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert seen["sniff_url"] == "https://hdtoday.to/watch/episode-9"
    assert seen["resolve_url"] == "https://cdn.example/hls/master.m3u8?token=x"
    assert seen["download_url"] == "https://cdn.example/hls/master.m3u8?token=x"
    assert seen["resolve_headers"] == {"Referer": "https://hdtoday.to/watch/episode-9"}
    assert seen["download_headers"] == {"Referer": "https://hdtoday.to/watch/episode-9"}
    await context.shutdown()


@pytest.mark.asyncio
async def test_page_sniff_disabled_skips_sniffing(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    context.settings.set("streaming.page_sniff", False)
    sniff_called = {"called": False}

    async def fake_sniff(self, url, headers):
        sniff_called["called"] = True
        return None

    monkeypatch.setattr(DownloadManager, "_sniff_stream_page", fake_sniff)
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: fake_info(title="Plain Title"),
    )

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        path = save_dir / (filename or "Plain Title.mp4")
        path.write_bytes(b"x" * 999)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add("https://hdtoday.to/watch/episode-9")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert not sniff_called["called"]
    await context.shutdown()


@pytest.mark.asyncio
async def test_page_sniff_skips_manifest_urls(tmp_path, monkeypatch):
    """A URL that already ends in .m3u8/.mpd is never sniffed for another one."""
    context = make_context(tmp_path)
    sniff_called = {"called": False}

    async def fake_sniff(self, url, headers):
        sniff_called["called"] = True
        return None

    monkeypatch.setattr(DownloadManager, "_sniff_stream_page", fake_sniff)
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream",
        lambda url, quality, **kwargs: fake_info(title="Direct M3U8"),
    )

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        path = save_dir / (filename or "Direct M3U8.mp4")
        path.write_bytes(b"x" * 777)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add("https://cdn.example/hls/master.m3u8")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert not sniff_called["called"]
    await context.shutdown()


@pytest.mark.asyncio
async def test_page_sniff_none_falls_through_to_ydl(tmp_path, monkeypatch):
    """When sniffing finds no manifest (e.g. a Cloudflare challenge page), the
    original page URL flows into yt-dlp resolution unchanged."""
    context = make_context(tmp_path)
    seen = {}

    async def fake_sniff(self, url, headers):
        seen["sniff_url"] = url
        return None

    monkeypatch.setattr(DownloadManager, "_sniff_stream_page", fake_sniff)

    def fake_resolve(url, quality, **kwargs):
        seen["resolve_url"] = url
        return fake_info(title="Fallback Title")

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        seen["download_url"] = url
        path = save_dir / (filename or "Fallback Title.mp4")
        path.write_bytes(b"x" * 456)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream", fake_resolve
    )
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add("https://hdtoday.to/watch/episode-9")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert seen["resolve_url"] == "https://hdtoday.to/watch/episode-9"
    assert seen["download_url"] == "https://hdtoday.to/watch/episode-9"
    await context.shutdown()


@pytest.mark.asyncio
async def test_page_sniff_exposed_referer_cookie_header(tmp_path, monkeypatch):
    """The sniffed page URL becomes the Referer header for the HLS download."""
    context = make_context(tmp_path)

    async def fake_sniff(self, url, headers):
        return SniffResult(
            manifest_url="https://cdn.example/hls/master.m3u8",
            page_url=url,
            referer="https://hdtoday.to/watch/episode-9",
        )

    monkeypatch.setattr(DownloadManager, "_sniff_stream_page", fake_sniff)

    def fake_resolve(url, quality, **kwargs):
        return fake_info(title="Referer Title")

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        assert headers.get("Referer") == "https://hdtoday.to/watch/episode-9"
        path = save_dir / (filename or "Referer Title.mp4")
        path.write_bytes(b"x" * 333)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream", fake_resolve
    )
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add("https://hdtoday.to/watch/episode-9")
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    await context.shutdown()


@pytest.mark.asyncio
async def test_page_sniff_recompiles_page_when_partial_resume(tmp_path, monkeypatch):
    """A partial file must disable page sniffing so yt-dlp can resume the same
    manifest stream without re-resolution."""
    context = make_context(tmp_path)
    save_dir = Path(context.settings.get("downloads.default_directory", ""))
    partial = save_dir / "Partial.mp4.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"half")

    sniff_called = {"called": False}

    async def fake_sniff(self, url, headers):
        sniff_called["called"] = True
        return None

    monkeypatch.setattr(DownloadManager, "_sniff_stream_page", fake_sniff)

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None):
        path = save_dir / filename
        path.write_bytes(b"x" * 200)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add(
        "https://hdtoday.to/watch/episode-9",
        filename="Partial.mp4",
        save_dir=str(save_dir),
    )
    context.manager.pause(download.id)
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert not sniff_called["called"]
    await context.shutdown()


@pytest.mark.asyncio
async def test_quality_selector_and_merge_reach_streaming(tmp_path, monkeypatch):
    """The per-download YouTube selector chosen by the quality dialog reaches
    resolve and download instead of the global streaming.quality setting."""
    context = make_context(tmp_path)
    context.settings.set("streaming.quality", "best")
    seen = {}

    def fake_resolve(url, quality, **kwargs):
        seen["selector"] = kwargs.get("format_selector")
        return fake_info(title="YT")

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None, format_selector=None, merge_output_format=None):
        seen["dl_selector"] = format_selector
        seen["merge"] = merge_output_format
        path = save_dir / (filename or "YT.mp4")
        path.write_bytes(b"x" * 100)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream", fake_resolve
    )
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_quality="1080",
        youtube_height=1080,
        youtube_format="137+140",
        youtube_format_selector="bv[height<=1080]+ba/b",
        youtube_merge_output_format="mp4",
        youtube_format_label="MP4 · 1080p",
        youtube_requested_quality="1080p",
    )
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert seen["selector"] == "bv[height<=1080]+ba/b"
    assert seen["dl_selector"] == "bv[height<=1080]+ba/b"
    assert seen["merge"] == "mp4"
    await context.shutdown()


@pytest.mark.asyncio
async def test_requested_quality_only_derives_standalone_selector(tmp_path, monkeypatch):
    """A playlist/settings-default row (quality but no selector) gets a
    standalone selector built from the requested quality label."""
    context = make_context(tmp_path)
    seen = {}

    def fake_resolve(url, quality, **kwargs):
        seen["selector"] = kwargs.get("format_selector")
        return fake_info(title="Playlist Video")

    def fake_download(url, save_dir, quality, progress_cb, cancel_event, cookies=None, headers=None, filename=None, format_selector=None, merge_output_format=None):
        seen["dl_selector"] = format_selector
        path = save_dir / (filename or "Playlist Video.mp4")
        path.write_bytes(b"x" * 100)
        return path

    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.resolve_stream", fake_resolve
    )
    monkeypatch.setattr(
        "magnetoclip.core.downloads.manager.download_stream", fake_download
    )

    download = context.manager.add(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_quality="720",
        youtube_requested_quality="720",
    )
    context.manager.start(download.id)

    await wait_for_status(context.manager, download.id, "completed")
    assert seen["selector"] == "bv[height<=720]+ba/b"
    assert seen["dl_selector"] == "bv[height<=720]+ba/b"
    await context.shutdown()
