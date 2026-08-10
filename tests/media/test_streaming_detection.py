"""Streaming media detection: which URLs route through the yt-dlp pipeline."""

from __future__ import annotations

from magnetoclip.media.streaming import (
    is_audio_platform,
    is_streaming_url,
)


def test_plain_file_urls_are_not_streaming():
    assert not is_streaming_url("https://example.com/file.zip")
    assert not is_streaming_url("https://cdn.example.com/reports/2026.pdf")
    assert not is_streaming_url("https://dl.example.com/big/archive.tar.gz")


def test_plain_file_urls_with_query_are_not_streaming():
    assert not is_streaming_url("https://cdn.example.com/app.exe?token=abc&v=2")


def test_youtube_is_streaming_video():
    assert is_streaming_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert is_streaming_url("https://youtu.be/dQw4w9WgXcQ")
    assert is_streaming_url("https://music.youtube.com/watch?v=abc")
    assert not is_audio_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_vimeo_and_dailymotion_are_streaming():
    assert is_streaming_url("https://vimeo.com/76979871")
    assert is_streaming_url("https://www.dailymotion.com/video/xabc")
    assert not is_audio_platform("https://vimeo.com/76979871")


def test_audio_platforms_detected():
    assert is_streaming_url("https://soundcloud.com/user/track")
    assert is_audio_platform("https://soundcloud.com/user/track")
    assert is_audio_platform("https://bandcamp.com/album/x")
    assert is_audio_platform("https://open.spotify.com/track/abc")


def test_manifest_urls_are_streaming():
    assert is_streaming_url("https://cdn.example.com/video/index.m3u8")
    assert is_streaming_url("https://cdn.example.com/video/hd.mpd")
    assert is_streaming_url("https://cdn.example.com/master.m3u8?token=1")


def test_non_streaming_hosts_with_media_paths():
    # media-looking paths on a generic CDN are NOT streamed (no extractor)
    assert not is_streaming_url("https://example.com/videos/clip.mp4")
    assert not is_streaming_url("https://example.com/audio/podcast.mp3")
