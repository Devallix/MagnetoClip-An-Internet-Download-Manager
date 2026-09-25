"""Regression tests for the in-app preview dialog.

The video case used to be driven by ``QVideoWidget``. On the affected Qt 6.11
Windows build that widget never paints a frame, so a previewed clip showed a
black rectangle while its audio played. These tests pin the replacement: frames
reach a surface that actually paints them.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget

from magnetoclip.services.preview import proxy as preview_proxy
from magnetoclip.services.preview.resolver import PreviewType
from magnetoclip.ui.dialogs.preview import (
    PreviewDialog,
    _VideoFrameSink,
    _VideoSurface,
)

_RED = "#ff0000"
_RED_PIXEL = 0xFFFF0000
_BLACK_PIXEL = 0xFF000000


def _red_frame(width: int = 64, height: int = 36) -> QVideoFrame:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(_RED))
    return QVideoFrame(image)


def _distinct_pixels(pixmap, step: int = 4) -> set[int]:
    image = pixmap.toImage()
    return {
        image.pixel(x, y)
        for y in range(0, max(image.height(), 1), step)
        for x in range(0, max(image.width(), 1), step)
    }


def test_video_surface_paints_the_decoded_frame(qapp):
    """A frame handed to the surface must be visible, not a black box."""
    surface = _VideoSurface()
    surface.resize(200, 200)

    surface.set_frame(_red_frame())

    assert surface.has_frame()
    assert surface.paints_with_frame == 0, "painting happens on repaint, not on set"
    pixels = _distinct_pixels(surface.grab())
    assert _RED_PIXEL in pixels, "decoded frame was not painted"
    assert _BLACK_PIXEL in pixels, "letterbox bars should stay black"


def test_video_surface_is_black_before_any_frame(qapp):
    surface = _VideoSurface()
    surface.resize(160, 120)

    assert not surface.has_frame()
    assert _distinct_pixels(surface.grab()) == {_BLACK_PIXEL}
    assert surface.paints_with_frame == 0


def test_video_surface_letterboxes_and_preserves_aspect(qapp):
    """A wide frame in a square surface fills the middle band only."""
    surface = _VideoSurface()
    surface.resize(200, 200)
    surface.set_frame(_red_frame(64, 16))  # 4:1

    image = surface.grab().toImage()
    assert image.pixel(100, 100) == _RED_PIXEL, "centre should be video"
    assert image.pixel(100, 2) == _BLACK_PIXEL, "top should be padded"
    assert image.pixel(100, 197) == _BLACK_PIXEL, "bottom should be padded"


def test_video_surface_clear_frame_empties_it(qapp):
    surface = _VideoSurface()
    surface.resize(120, 90)
    surface.set_frame(_red_frame())
    surface.grab()

    surface.clear_frame()

    assert not surface.has_frame()
    assert _distinct_pixels(surface.grab()) == {_BLACK_PIXEL}


def test_video_sink_forwards_frames_from_the_signal(qapp):
    """Frames must come off ``videoFrameChanged``.

    PySide6 does not dispatch the ``QVideoSink`` virtual
    ``onVideoFrameChanged``, so an override silently never runs and the
    preview would stay black again.
    """
    surface = _VideoSurface()
    surface.resize(120, 90)
    sink = _VideoFrameSink(surface)

    sink.videoFrameChanged.emit(_red_frame())

    assert sink.frames == 1
    assert surface.has_frame()
    assert _RED_PIXEL in _distinct_pixels(surface.grab())


def test_video_dialog_renders_through_the_sink(qtbot, tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"not a real clip")

    dialog = PreviewDialog(None, str(target), PreviewType.VIDEO)
    qtbot.addWidget(dialog)

    assert isinstance(dialog.video_surface, _VideoSurface)
    assert isinstance(dialog.video_sink, _VideoFrameSink)
    assert dialog.player.videoOutput() is dialog.video_sink
    # The broken widget must not creep back in.
    assert dialog.findChild(QVideoWidget) is None
    assert not hasattr(dialog, "video_widget")


def test_video_dialog_reports_a_file_without_a_video_track(qtbot):
    dialog = PreviewDialog(None, "clip.mp4", PreviewType.VIDEO)
    qtbot.addWidget(dialog)
    assert dialog.media_error_label.isHidden()

    dialog._reject_video_without_track()

    assert "no video track" in dialog.media_error_label.text().lower()
    assert not dialog.media_error_label.isHidden()


def test_playback_error_is_not_overwritten_by_the_time_label(qtbot):
    """The elapsed-time label refreshes several times a second.

    Reporting errors on that label made them disappear almost instantly, so a
    genuine decode failure was never actually visible to the user.
    """
    dialog = PreviewDialog(None, "clip.mp4", PreviewType.AUDIO)
    qtbot.addWidget(dialog)
    assert dialog.media_error_label.isHidden()

    dialog._on_player_error(QMediaPlayer.Error.FormatError, "codec missing")
    dialog._on_position_changed()

    assert "codec missing" in dialog.media_error_label.text()
    assert not dialog.media_error_label.isHidden()

    dialog._play_pressed()
    assert dialog.media_error_label.isHidden()


def test_video_dialog_defers_playback_until_shown(qtbot, tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"not a real clip")

    dialog = PreviewDialog(None, str(target), PreviewType.VIDEO)
    qtbot.addWidget(dialog)
    assert dialog._autoplay is True, "playback must start on show, not in __init__"

    dialog.show()
    qtbot.wait(50)

    assert dialog._autoplay is False


def test_undecodable_codec_starts_a_proxy_instead_of_playing(qtbot, tmp_path, monkeypatch):
    """An AV1 source must be transcoded, never handed to QMediaPlayer."""
    target = tmp_path / "clip.mkv"
    target.write_bytes(b"not a real clip")
    monkeypatch.setattr(preview_proxy, "cached_proxy", lambda *a, **k: None)
    monkeypatch.setattr(
        preview_proxy, "needs_proxy", lambda codec: codec == "av1", raising=True
    )

    dialog = PreviewDialog(None, str(target), PreviewType.VIDEO)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        dialog, "_maybe_build_proxy", lambda: setattr(dialog, "_proxy_tried", True)
    )
    dialog._on_codec_verdict("unsupported")

    assert dialog._proxy_tried is True
    # The source must not have been played.
    assert dialog.player.source().isEmpty() or dialog._proxy_path is None


def test_playable_codec_starts_normal_playback(qtbot, tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"not a real clip")

    dialog = PreviewDialog(None, str(target), PreviewType.VIDEO)
    qtbot.addWidget(dialog)
    played = []
    dialog.player.play = lambda: played.append(True)

    dialog._on_codec_verdict("playable")

    assert played == [True]
    assert dialog._proxy_tried is False


def test_close_stops_playback_and_releases_the_frame(qtbot, tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"not a real clip")

    dialog = PreviewDialog(None, str(target), PreviewType.VIDEO)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(30)
    dialog.video_surface.set_frame(_red_frame())
    assert dialog.video_surface.has_frame()

    dialog.close()

    assert not dialog.video_surface.has_frame()
    assert dialog._check_worker is None
    assert dialog._proxy_worker is None
