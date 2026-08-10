"""Manager integration: media detection on add and after completion."""

from __future__ import annotations

import asyncio

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.core.events.bus import Events

from tests.support.http_server import PayloadServer


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    return context


def test_add_sets_detected_type(tmp_path):
    context = make_context(tmp_path)
    download = context.manager.add(
        "https://example.com/song.mp3", filename="song.mp3"
    )
    assert download.detected_type == "audio"
    context.database.close()


@pytest.mark.asyncio
async def test_completion_posts_media_event(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    detected: list[dict] = []
    context.events.connect(Events.MEDIA_DETECTED, detected.append)

    class _NoProbe:
        @property
        def available(self) -> bool:
            return False

    monkeypatch.setattr(
        "magnetoclip.media.ffmpeg.FFmpegLocator", lambda *a, **k: _NoProbe()
    )

    with PayloadServer(b"x" * 2048) as server:
        download = context.manager.add(server.url, filename="show.S01E02.720p.mkv")
        context.manager.start(download.id)
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline and not detected:
            await asyncio.sleep(0.02)
        assert detected, "MEDIA_DETECTED never fired"
        payload = detected[0]
        assert payload["detected_type"] == "video"
        assert payload["media"]["season"] == 1
        assert payload["media"]["episode"] == 2
        assert payload["media"]["resolution"] == "720p"
    await context.shutdown()
