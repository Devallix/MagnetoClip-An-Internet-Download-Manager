"""Streaming media detection: which URLs route through the yt-dlp pipeline."""

from __future__ import annotations

import pytest

from magnetoclip.media.streaming import (
    _apply_impersonation,
    _apply_manifest_token,
    _impersonation_available,
    _write_cookiefile,
    is_audio_platform,
    is_hls_or_dash_url,
    is_streaming_site_url,
    is_streaming_url,
)

import magnetoclip.media.streaming as streaming_mod


def test_apply_impersonation_when_unavailable(monkeypatch):
    monkeypatch.setattr(
        "magnetoclip.media.streaming._impersonation_available", lambda: False
    )
    params = {"extractor_args": {"youtube": {"player_client": ["android"]}}}
    _apply_impersonation(params)
    assert "impersonate" not in params
    assert params["extractor_args"] == {"youtube": {"player_client": ["android"]}}


def test_apply_impersonation_when_available(monkeypatch):
    if not _impersonation_available():
        import pytest

        pytest.skip("curl_cffi not installed; impersonation is a no-op")
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.impersonate import ImpersonateTarget

    params = {"extractor_args": {"youtube": {"player_client": ["android"]}}}
    _apply_impersonation(params)
    assert isinstance(params["impersonate"], ImpersonateTarget)
    assert params["extractor_args"]["generic"] == {"impersonate": ["chrome"]}
    assert params["extractor_args"]["youtube"]["player_client"] == ["android"]
    # The full param set must construct a working YoutubeDL (impersonation
    # enabled) without raising the "target not available" guard.
    ydl = YoutubeDL({**params, "quiet": True})
    assert ydl is not None


def test_apply_manifest_token_seeds_generic_query_args():
    """Tokenized master playlists must carry ?token= onto every child request;
    _apply_manifest_token seeds yt-dlp's variant/fragment/key_query args."""
    params = {}
    _apply_manifest_token(
        params, "https://fetch.nexabloom.top/anime/x/y/master.m3u8?token=TTT"
    )
    assert params["extractor_args"]["generic"]["variant_query"] == ["token=TTT"]
    assert params["extractor_args"]["generic"]["fragment_query"] == ["token=TTT"]
    assert params["extractor_args"]["generic"]["key_query"] == ["token=TTT"]


def test_apply_manifest_token_keeps_existing_generic_args():
    params = {"extractor_args": {"generic": {"impersonate": ["chrome"]}}}
    _apply_manifest_token(
        params, "https://fetch.nexabloom.top/anime/x/y/master.m3u8?token=T"
    )
    assert params["extractor_args"]["generic"]["impersonate"] == ["chrome"]
    assert params["extractor_args"]["generic"]["fragment_query"] == ["token=T"]


def test_apply_manifest_token_noop_without_query():
    params = {"extractor_args": {"generic": {"impersonate": ["chrome"]}}}
    _apply_manifest_token(params, "https://cdn.example/hls/master.m3u8")
    assert params["extractor_args"]["generic"] == {"impersonate": ["chrome"]}


class _CapturingYdl:
    """Stand-in for yt_dlp.YoutubeDL that records the params it received."""

    captured: list = []
    info = {}
    fail_count = 0
    fail_exc = RuntimeError("boom")

    def __init__(self, params, *_args, **_kwargs):
        _CapturingYdl.captured.append(params)
        self.params = params

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if _CapturingYdl.fail_count > 0:
            _CapturingYdl.fail_count -= 1
            raise _CapturingYdl.fail_exc
        return self._info


def _patch_streaming(monkeypatch, info=None, *, ffmpeg=True):
    import yt_dlp

    _CapturingYdl.captured.clear()
    if info is None:
        info = {
            "title": "Probe",
            "ext": "mp4",
            "vcodec": "avc1.42001E",
            "acodec": "mp4a.40.2",
            "formats": [],
        }
    _CapturingYdl._info = info
    monkeypatch.setattr(streaming_mod, "_detect_browser", lambda: None)
    monkeypatch.setattr(streaming_mod, "_ffmpeg_available", lambda: ffmpeg)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _CapturingYdl)
    return streaming_mod


def test_resolve_stream_passes_selector_without_android_cap(monkeypatch):
    """The chosen YouTube format selector reaches yt-dlp verbatim, and the
    forced ``android`` player client (which caps YouTube to 360p) is gone so
    the default web ladder serves the selected resolution."""
    from magnetoclip.media.streaming import resolve_stream

    _patch_streaming(monkeypatch)
    resolve_stream(
        "https://www.youtube.com/watch?v=ABC",
        "best",
        format_selector="bv[height<=1080]+ba/b",
        merge_output_format="mp4",
    )
    params = _CapturingYdl.captured[-1]
    assert params["format"] == "bv[height<=1080]+ba/b"
    assert params["merge_output_format"] == "mp4"
    extractor_args = params.get("extractor_args", {})
    assert "youtube" not in extractor_args
    assert set(extractor_args) <= {"generic"}


def test_download_stream_uses_selector_and_keeps_full_ladder(monkeypatch, tmp_path):
    """The download request carries the merge selector and merge container, and
    does not narrow the ladder with a player-client override."""
    from magnetoclip.media.streaming import download_stream

    _patch_streaming(monkeypatch)
    out = tmp_path / "v.mp4"
    info = {
        "title": "Probe",
        "ext": "mp4",
        "vcodec": "avc1.42001E",
        "acodec": "none",
        "requested_downloads": [{"filepath": str(out)}],
    }
    _CapturingYdl._info = info
    result = download_stream(
        "https://www.youtube.com/watch?v=ABC",
        tmp_path,
        "best",
        format_selector="bv[height<=1080]+ba/b",
        merge_output_format="mp4",
    )
    assert str(result) == str(out)
    params = _CapturingYdl.captured[-1]
    assert params["format"] == "bv[height<=1080]+ba/b"
    assert params["merge_output_format"] == "mp4"
    assert params["http_chunk_size"] == 5 * 1024 * 1024
    extractor_args = params.get("extractor_args", {})
    assert "youtube" not in extractor_args
    assert set(extractor_args) <= {"generic"}


def test_is_stream_retryable_error_matches_blocks_and_403():
    """YouTube session failures that warrant a fresh-extract retry."""
    from magnetoclip.media.streaming import _is_stream_retryable_error

    assert _is_stream_retryable_error("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert _is_stream_retryable_error("unable to download video data: http error 403:forbidden")
    assert _is_stream_retryable_error("ERROR: [youtube] abc: Requested format is not available.")
    assert _is_stream_retryable_error("No video formats found, please report this issue")
    assert _is_stream_retryable_error("ERROR: [youtube] abc: The page needs to be reloaded.")
    assert _is_stream_retryable_error('ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot')


def test_download_stream_retries_same_client_with_backoff_on_403(monkeypatch, tmp_path):
    """A mid-stream 403 (YouTube throttling the session) keeps the default
    client on retry - the throttle is per-IP, so a fresh extraction of the same
    ladder resumes the partial rather than rotating to a (likely different)
    format, and waits out the block with a short backoff."""
    from magnetoclip.media.streaming import download_stream

    _patch_streaming(monkeypatch)
    sleeps = []
    monkeypatch.setattr(
        streaming_mod.time, "sleep", lambda delay: sleeps.append(delay)
    )
    out = tmp_path / "v.mp4"
    info = {
        "title": "Probe",
        "ext": "mp4",
        "vcodec": "avc1.42001E",
        "acodec": "none",
        "requested_downloads": [{"filepath": str(out)}],
    }
    _CapturingYdl._info = info
    _CapturingYdl.fail_count = 2
    _CapturingYdl.fail_exc = RuntimeError(
        "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    )
    try:
        result = download_stream(
            "https://www.youtube.com/watch?v=ABC",
            tmp_path,
            "best",
            format_selector="bv[height<=1080]+ba/b",
            merge_output_format="mp4",
        )
        assert str(result) == str(out)
        captures = _CapturingYdl.captured
        assert len(captures) == 3
        for params in captures:
            assert params["format"] == "bv[height<=1080]+ba/b"
            assert "youtube" not in params.get("extractor_args", {})
        assert sleeps and all(d > 0 for d in sleeps)
    finally:
        _CapturingYdl.fail_count = 0


def test_download_stream_rotates_to_android_vr_on_extract_block(monkeypatch, tmp_path):
    """An extract-level block ("Requested format is not available") rotates the
    player client on retry while keeping the merge selector."""
    from magnetoclip.media.streaming import download_stream

    _patch_streaming(monkeypatch)
    monkeypatch.setattr(streaming_mod.time, "sleep", lambda delay: None)
    out = tmp_path / "v.mp4"
    info = {
        "title": "Probe",
        "ext": "mp4",
        "vcodec": "avc1.42001E",
        "acodec": "none",
        "requested_downloads": [{"filepath": str(out)}],
    }
    _CapturingYdl._info = info
    _CapturingYdl.fail_count = 3
    _CapturingYdl.fail_exc = RuntimeError(
        "ERROR: [youtube] abc: Requested format is not available. "
        "Use --list-formats for a list of available formats"
    )
    try:
        result = download_stream(
            "https://www.youtube.com/watch?v=ABC",
            tmp_path,
            "best",
            format_selector="bv[height<=1080]+ba/b",
            merge_output_format="mp4",
        )
        assert str(result) == str(out)
        captures = _CapturingYdl.captured
        assert len(captures) == 4
        for params in captures[:3]:
            assert "youtube" not in params.get("extractor_args", {})
        assert captures[3]["extractor_args"]["youtube"]["player_client"] == ["android_vr"]
    finally:
        _CapturingYdl.fail_count = 0


def test_download_stream_fails_fast_on_non_retryable_error(monkeypatch, tmp_path):
    """Genuine (non-session) errors surface immediately instead of burning the
    client-rotation attempts on a doomed video."""
    from magnetoclip.media.streaming import download_stream

    _patch_streaming(monkeypatch)
    fatal = RuntimeError("ERROR: [generic] Unsupported URL")
    _CapturingYdl._info = {"title": "Probe", "ext": "mp4"}
    _CapturingYdl.fail_count = 1
    _CapturingYdl.fail_exc = fatal
    try:
        with pytest.raises(RuntimeError) as excinfo:
            download_stream(
                "https://www.youtube.com/watch?v=ABC",
                tmp_path,
                "best",
                format_selector="bv[height<=1080]+ba/b",
            )
        assert excinfo.value is fatal
        assert len(_CapturingYdl.captured) == 1
    finally:
        _CapturingYdl.fail_count = 0


def test_apply_manifest_token_noop_for_non_manifest():
    params = {}
    _apply_manifest_token(params, "https://www.youtube.com/watch?v=abc")
    assert params == {}


def test_apply_manifest_token_does_not_drop_query_with_dot():
    """Query values are opaque strings (they may contain base64 dots/padding)."""
    params = {}
    query = "token=abc.def=="
    _apply_manifest_token(
        params, f"https://fetch.nexabloom.top/a/b/master.m3u8?{query}"
    )
    assert params["extractor_args"]["generic"]["fragment_query"] == [query]


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


def test_streaming_site_urls_are_streaming():
    assert is_streaming_url("https://anikai.watch/anime/one-piece")
    assert is_streaming_url("https://anikai.vip/episode/chiikawa-episode-165")
    assert is_streaming_url("https://anikai.rip/anime/dorohedoro-season-2")
    assert is_streaming_url("https://hdtoday24.at/home")
    assert is_streaming_url("https://hdtodayz.app/watch/movie/123/abc")
    assert is_streaming_url("https://4thdmovie.com/movie/evil-dead-burn")


def test_streaming_site_url_detection():
    assert is_streaming_site_url("https://anikai.watch/")
    assert is_streaming_site_url("https://www.anikai.watch/anime/x")
    assert is_streaming_site_url("https://hdtoday24.at/movie/title")
    assert is_streaming_site_url("https://animekai.org.lv/oishinbo-episode-26")
    assert not is_streaming_site_url("https://youtube.com/watch?v=abc")
    assert not is_streaming_site_url("https://example.com/anime")


def test_hls_dash_url_detection():
    assert is_hls_or_dash_url("https://cdn.example.com/hls/master.m3u8")
    assert is_hls_or_dash_url("https://cdn.example.com/dash/hd.mpd?token=1")
    assert is_hls_or_dash_url("https://cdn.example.com/stream/playlist.m3u8#frag")
    assert not is_hls_or_dash_url("https://example.com/video.mp4")
    assert not is_hls_or_dash_url("https://example.com/page")
    assert not is_hls_or_dash_url("")


def test_streaming_site_manifest_url_is_streaming():
    # A CDN m3u8 URL used by a streaming site counts as a manifest URL even
    # without the site host.
    assert is_streaming_url("https://cdn.example.net/hls/abc123/index.m3u8")


def test_write_cookiefile_from_dict(tmp_path):
    path = _write_cookiefile(
        "https://www.youtube.com/watch?v=abc",
        {"SID": "one", "HSID": "two"},
    )
    try:
        assert path is not None
        content = path.read_text(encoding="utf-8")
        assert content.splitlines()[0] == "# Netscape HTTP Cookie File"
        assert ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tone" in content
        assert "HSID\ttwo" in content
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def test_write_cookiefile_accepts_header_string(tmp_path):
    path = _write_cookiefile(
        "https://example.com/x",
        "a=1; b=2",
    )
    try:
        assert path is not None
        content = path.read_text(encoding="utf-8")
        assert ".example.com\tTRUE\t/\tTRUE\t2147483647\ta\t1" in content
        assert "b\t2" in content
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def test_write_cookiefile_none_without_cookies():
    assert _write_cookiefile("https://example.com/x", None) is None
    assert _write_cookiefile("https://example.com/x", "") is None
