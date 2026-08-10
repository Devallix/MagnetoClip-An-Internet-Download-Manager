"""Media detection, metadata, and ffmpeg bridge tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from magnetoclip.media.detect import (
    category_for_type,
    detect_type,
    is_streaming_url,
)
from magnetoclip.media.ffmpeg import (
    FFmpegNotFoundError,
    FFmpegLocator,
    _summarize,
    probe,
)
from magnetoclip.media.metadata import clean_title, extract_from_filename


# ----- detection -----


def test_detect_by_extension():
    assert detect_type(filename="movie.mkv") == "video"
    assert detect_type(filename="song.mp3") == "audio"
    assert detect_type(filename="photo.jpg") == "image"
    assert detect_type(filename="doc.pdf") == "document"
    assert detect_type(filename="pack.zip") == "archive"
    assert detect_type(filename="setup.exe") == "software"
    assert detect_type(filename="book.epub") == "ebook"
    assert detect_type(filename="captions.srt") == "subtitle"
    assert detect_type(filename="font.ttf") == "font"
    assert detect_type(filename="weird.xyz") == "unknown"


def test_detect_content_type_wins():
    assert detect_type(filename="movie.mkv", content_type="video/mp4") == "video"
    assert detect_type(filename="file.bin", content_type="image/png") == "image"
    assert detect_type(filename="file.bin", content_type="application/pdf") == "document"
    assert detect_type(filename="x", content_type="application/zip") == "archive"
    assert detect_type(filename="x", content_type="application/octet-stream") == "unknown"


def test_detect_streaming_url():
    assert is_streaming_url("https://example.com/stream/master.m3u8")
    assert is_streaming_url("https://example.com/stream.mpd?token=1")
    assert not is_streaming_url("https://example.com/file.mp4")
    assert detect_type(url="https://example.com/live/index.m3u8") == "video"


def test_category_mapping():
    assert category_for_type("video") == "Videos"
    assert category_for_type("audio") == "Music"
    assert category_for_type("archive") == "Archives"
    assert category_for_type("nonsense") == "Other"


# ----- metadata -----


def test_extract_artist_title():
    metadata = extract_from_filename("ACDC - Back In Black.mp3")
    assert metadata["artist"] == "ACDC"
    assert metadata["title"] == "Back In Black"


def test_extract_episode_and_resolution():
    metadata = extract_from_filename("Show Name.S01E02.1080p.BluRay.mkv")
    assert metadata["season"] == 1
    assert metadata["episode"] == 2
    assert metadata["resolution"] == "1080p"
    assert metadata["source"] == "bluray"


def test_extract_year():
    assert extract_from_filename("Album (2019).flac")["year"] == 2019


def test_clean_title():
    metadata = extract_from_filename("Show Name.S01E02.1080p.BluRay.mkv")
    assert "Show Name" in clean_title(metadata)
    assert "S01E02" in clean_title(metadata)
    assert "1080p" in clean_title(metadata)


def test_extract_empty():
    assert extract_from_filename(None) == {}
    assert extract_from_filename("") == {}


# ----- ffmpeg bridge -----


def test_locator_unavailable_when_not_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    locator = FFmpegLocator()
    assert locator.available is False
    with pytest.raises(FFmpegNotFoundError):
        locator.probe_path()


def test_probe_returns_none_without_ffprobe(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    locator = FFmpegLocator()

    async def _run():
        return await probe("x.mp4", locator)

    assert asyncio.run(_run()) is None


def test_probe_graceful_on_missing_binary(tmp_path):
    locator = FFmpegLocator(ffprobe_path=str(tmp_path / "does_not_exist.exe"))

    async def _run():
        return await probe("x.mp4", locator)

    assert asyncio.run(_run()) is None


def test_probe_with_fake_binary(tmp_path, monkeypatch):
    class _FakeProcess:
        returncode = 0

        def __init__(self, output: bytes) -> None:
            self._output = output

        async def communicate(self):
            return self._output, b""

    fake_output = json.dumps(
        {
            "format": {"format_name": "matroska", "duration": "9.5", "bit_rate": "1234"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                {"codec_type": "audio", "codec_name": "aac", "channels": 2},
            ],
        }
    ).encode()

    async def _fake_exec(*_args, **_kwargs):
        return _FakeProcess(fake_output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    locator = FFmpegLocator(ffprobe_path=str(tmp_path / "fake_probe"))

    async def _run():
        return await probe("x.mp4", locator)

    info = asyncio.run(_run())
    assert info is not None
    assert info["format_name"] == "matroska"
    assert info["duration"] == 9.5
    assert info["codec"] == "h264"
    assert info["width"] == 1920
    assert info["height"] == 1080
    assert info["audio_codec"] == "aac"
    assert info["channels"] == 2


def test_summarize_skips_unknown():
    raw = {
        "format": {"duration": "N/A", "bit_rate": "0"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": "abc"}],
    }
    summary = _summarize(raw)
    assert "duration" not in summary
    assert "bit_rate" not in summary
    assert "width" not in summary
    assert summary["codec"] == "h264"


def test_probe_bad_json_returns_none(tmp_path, monkeypatch):
    class _BadProcess:
        returncode = 0

        async def communicate(self):
            return b"not json", b""

    async def _fake_exec(*_args, **_kwargs):
        return _BadProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    locator = FFmpegLocator(ffprobe_path=str(tmp_path / "bad_probe"))

    async def _run():
        return await probe("x.mp4", locator)

    assert asyncio.run(_run()) is None
