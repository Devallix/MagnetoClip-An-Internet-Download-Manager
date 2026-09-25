"""Tests for the preview proxy used when a codec cannot be decoded.

Qt's bundled FFmpeg has no software AV1 decoder, so an AV1 file yields audio but
no video frames. The preview responds by transcoding a small H.264 proxy once
and replaying that instead.
"""

from __future__ import annotations

import os
import time

import pytest

from magnetoclip.media import ffmpeg
from magnetoclip.media.ffmpeg import (
    PREVIEW_ENCODERS,
    FFmpegLocator,
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
        # libx264 takes -crf; libopenh264 and mpeg4 both reject it.
        if encoder == "libx264":
            assert "-crf" in command
        else:
            assert "-crf" not in command


def test_gpl_encoder_is_never_picked_for_an_lgpl_build(monkeypatch):
    """Bundling libx264 would make the proprietary app GPL, so skip it.

    The vendored LGPL build encodes H.264 with libopenh264 (BSD) instead, and
    the GPL encoder must not even be attempted there.
    """
    monkeypatch.setattr(ffmpeg, "_ENCODER_CACHE", {})
    monkeypatch.setattr(
        ffmpeg,
        "_supported_encoders",
        lambda path: {"libopenh264", "mpeg4", "libdav1d", "aac"},
    )

    assert ffmpeg.preview_encoders("ffmpeg") == ("libopenh264", "mpeg4")


def test_a_gpl_ffmpeg_still_gets_the_better_encoder(monkeypatch):
    """A user-supplied GPL ffmpeg should not be downgraded unnecessarily."""
    monkeypatch.setattr(ffmpeg, "_ENCODER_CACHE", {})
    monkeypatch.setattr(
        ffmpeg,
        "_supported_encoders",
        lambda path: {"libx264", "libopenh264", "mpeg4"},
    )

    assert ffmpeg.preview_encoders("ffmpeg") == (
        "libx264",
        "libopenh264",
        "mpeg4",
    )


def test_encoder_detection_falls_back_when_unreadable(monkeypatch):
    """An unusual build must still get a chance rather than losing previews."""
    monkeypatch.setattr(ffmpeg, "_ENCODER_CACHE", {})
    monkeypatch.setattr(ffmpeg, "_supported_encoders", lambda path: None)

    assert ffmpeg.preview_encoders("ffmpeg") == PREVIEW_ENCODERS


def test_encoder_detection_always_leaves_a_usable_encoder(monkeypatch):
    """Even a build with none of the known encoders must fall back to mpeg4."""
    monkeypatch.setattr(ffmpeg, "_ENCODER_CACHE", {})
    monkeypatch.setattr(ffmpeg, "_supported_encoders", lambda path: {"h264_nvenc"})

    assert ffmpeg.preview_encoders("ffmpeg") == ("mpeg4",)


def test_encoder_list_is_parsed_from_ffmpeg_output(monkeypatch):
    """The flags column is skipped; only real encoder names are collected."""
    captured = {}

    class _Result:
        stdout = (
            b"Encoders:\n"
            b" V..... libx264              H.264\n"
            b" V....D libopenh264          OpenH264 H.264\n"
            b" A....D aac                  AAC\n"
            b" ------ -------------------  ------\n"
        )

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(ffmpeg, "_ENCODER_CACHE", {})
    monkeypatch.setattr(ffmpeg.subprocess, "run", _run)

    found = ffmpeg._supported_encoders("C:/ffmpeg.exe")

    assert {"libx264", "libopenh264", "aac"} <= found
    assert "Encoders:" not in found
    assert "------" not in found
    assert captured["cmd"][0] == "C:/ffmpeg.exe"


def test_encoder_detection_result_is_cached(monkeypatch):
    """It costs a process launch, so it must not run twice per binary."""
    monkeypatch.setattr(ffmpeg, "_ENCODER_CACHE", {})
    calls = []

    class _Result:
        stdout = b" V..... libopenh264  OpenH264\n"

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(ffmpeg.subprocess, "run", _run)

    ffmpeg._supported_encoders("C:/ffmpeg.exe")
    ffmpeg._supported_encoders("C:/ffmpeg.exe")

    assert len(calls) == 1


def test_bundled_binary_wins_over_path(monkeypatch, tmp_path):
    """The vendored copy must take precedence so machines without ffmpeg work."""
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "ffmpeg.exe").write_bytes(b"stub")
    monkeypatch.setattr(ffmpeg, "bundled_bin_dir", lambda: bundled)

    locator = FFmpegLocator(ffmpeg_path=None)

    assert locator.ffmpeg_path == str(bundled / "ffmpeg.exe")
    assert locator.ffmpeg_available


def test_bundled_bin_dir_prefers_the_frozen_layout(monkeypatch, tmp_path):
    """Frozen builds put resources under _MEIPASS, not next to the source."""
    frozen = tmp_path / "frozen" / "magnetoclip" / "resources" / "bin"
    frozen.mkdir(parents=True)
    monkeypatch.setattr(ffmpeg.sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)

    assert ffmpeg.bundled_bin_dir() == frozen


def test_explicit_setting_still_overrides_the_bundled_binary(monkeypatch, tmp_path):
    """A user pointing at their own ffmpeg must keep winning."""
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "ffmpeg.exe").write_bytes(b"stub")
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "ffmpeg.exe").write_bytes(b"stub")
    monkeypatch.setattr(ffmpeg, "bundled_bin_dir", lambda: bundled)

    locator = FFmpegLocator(ffmpeg_path=str(custom / "ffmpeg.exe"))

    assert locator.ffmpeg_path == str(custom / "ffmpeg.exe")


def test_preview_works_with_only_ffmpeg_and_no_ffprobe(monkeypatch, tmp_path):
    """ffprobe is deliberately not bundled; detection must not depend on it.

    The no-frames watchdog covers codec detection, so the proxy path has to stay
    available on a machine that has no ffprobe at all.
    """
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "ffmpeg.exe").write_bytes(b"stub")
    monkeypatch.setattr(ffmpeg, "bundled_bin_dir", lambda: bundled)
    # Simulate a machine with no ffmpeg tooling on PATH.
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)

    locator = FFmpegLocator(ffmpeg_path=None)

    assert locator.available is False, "ffprobe is absent"
    assert locator.ffmpeg_available is True
    assert locator.ffmpeg() == str(bundled / "ffmpeg.exe")


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


def test_av1_is_routed_to_a_proxy_but_other_codecs_are_not():
    """Handing AV1 to Qt only floods the log and shows black.

    Its bundled FFmpeg has no software AV1 decoder, so the preview must
    transcode instead of attempting playback.
    """
    assert preview_proxy.needs_proxy("av1") is True
    assert preview_proxy.needs_proxy("AV1") is True
    # Codecs Qt does decode must keep playing natively.
    for codec in ("h264", "hevc", "vp9", "mpeg4", "mjpeg", "wmv3"):
        assert preview_proxy.needs_proxy(codec) is False


def test_unknown_codec_is_left_to_playback():
    """No probe result must not block normal playback."""
    assert preview_proxy.needs_proxy(None) is False
    assert preview_proxy.needs_proxy("") is False


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
