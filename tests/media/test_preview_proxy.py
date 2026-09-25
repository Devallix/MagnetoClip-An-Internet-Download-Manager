"""Tests for the preview proxy used when a codec cannot be decoded.

Qt's bundled FFmpeg has no software AV1 decoder, so an AV1 file yields audio but
no video frames. The preview responds by transcoding a small H.264 proxy once
and replaying that instead.
"""

from __future__ import annotations

import os
import time

import pytest

from magnetoclip.media.ffmpeg import (
    PREVIEW_ENCODERS,
    build_preview_proxy_command,
)
from magnetoclip.services.preview import proxy as preview_proxy


def test_proxy_command_targets_a_decoder_qt_can_handle():
    command = build_preview_proxy_command("ffmpeg", "in.mkv", "out.mkv")

    assert command[0] == "ffmpeg"
    assert "-c:v" in command
    encoder = command[command.index("-c:v") + 1]
    assert encoder == "libx264", "AV1 must be re-encoded to something decodable"
    # The video is downscaled and audio is copied, so the fallback does not
    # re-encode audio or produce a needlessly large file.
    assert "-vf" in command
    assert "scale=min(640\\,iw):-2" in command
    assert command[command.index("-c:a") + 1] == "copy"


def test_proxy_command_escapes_the_filter_comma():
    """ffmpeg splits filter args on commas, so the escape must survive."""
    command = build_preview_proxy_command("ffmpeg", "in.mkv", "out.mkv", max_width=320)

    scale = command[command.index("-vf") + 1]
    assert scale == "scale=min(320\\,iw):-2"


def test_proxy_command_caps_duration_when_asked():
    command = build_preview_proxy_command("ffmpeg", "in.mkv", "out.mkv", max_seconds=60)

    assert command[command.index("-t") + 1] == "60"


def test_proxy_command_covers_every_declared_encoder():
    """Each fallback encoder needs its own rate-control flags to be valid."""
    for encoder in PREVIEW_ENCODERS:
        command = build_preview_proxy_command(
            "ffmpeg", "in.mkv", "out.mkv", video_encoder=encoder
        )

        assert command[command.index("-c:v") + 1] == encoder
        # libx264 takes -crf, mpeg4 would reject it.
        if encoder == "mpeg4":
            assert "-crf" not in command
        else:
            assert "-crf" in command


def test_proxy_path_is_stable_and_invalidated_by_the_source(tmp_path):
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"first")

    first = preview_proxy.proxy_path(source, None)

    assert first == preview_proxy.proxy_path(source, None), "cache key must be stable"
    # Rewriting the file changes its mtime/size, so a stale proxy is not reused.
    source.write_bytes(b"second and longer")
    assert preview_proxy.proxy_path(source, None) != first


def test_cached_proxy_reports_a_missing_entry(tmp_path):
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"data")

    assert preview_proxy.cached_proxy(source, None) is None


def test_cached_proxy_finds_a_built_proxy(tmp_path, monkeypatch):
    source = tmp_path / "clip.mkv"
    source.write_bytes(b"data")
    monkeypatch.setattr(preview_proxy, "cache_dir", lambda context=None: tmp_path)
    built = preview_proxy.proxy_path(source, None)
    built.write_bytes(b"proxied")

    assert preview_proxy.cached_proxy(source, None) == built


def test_prune_keeps_the_newest_entries(tmp_path):
    old = tmp_path / "old.mkv"
    new = tmp_path / "new.mkv"
    for path, age in ((old, 10_000), (new, 0)):
        path.write_bytes(b"x")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    removed = preview_proxy.prune(tmp_path, keep=1)

    assert removed == 1
    assert new.exists()
    assert not old.exists()


def test_prune_drops_stale_entries_even_inside_the_cap(tmp_path):
    """A single ancient proxy should not be kept just for being under the cap."""
    stale = tmp_path / "stale.mkv"
    stale.write_bytes(b"x")
    stamp = time.time() - preview_proxy._PROXY_MAX_AGE_SECONDS - 60
    os.utime(stale, (stamp, stamp))

    removed = preview_proxy.prune(tmp_path, keep=8)

    assert removed == 1
    assert not stale.exists()


@pytest.mark.parametrize("width", [320, 640, 1280])
def test_proxy_width_is_never_up_scaled_above_the_source(width):
    """The filter's min() keeps small sources at their native size."""
    command = build_preview_proxy_command("ffmpeg", "in.mkv", "out.mkv", max_width=width)

    assert f"min({width}\\,iw)" in command[command.index("-vf") + 1]
