"""Tests for magneto:// command URI parsing and dispatch."""

from __future__ import annotations

import pytest

from magnetoclip.services.shell.commands import (
    apply_command,
    build_launch_uri,
    parse_command_uri,
)


class FakeWindow:
    def __init__(self):
        self.shown = []
        self.navs = []
        self.detected = []

    def show(self):
        self.shown.append("show")

    def raise_(self):
        self.shown.append("raise")

    def activateWindow(self):
        self.shown.append("activate")

    def _activate_nav(self, key):
        self.navs.append(key)

    def _open_detected(self):
        self.detected.append(True)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("magneto://open", {"action": "open"}),
        ("magneto://open-downloads", {"action": "open-downloads"}),
        ("magneto://open-detected", {"action": "open-detected"}),
        ("magneto://reveal?download=7", {"action": "reveal", "download_id": 7}),
        ("magneto://reveal?download=abc", {"action": "reveal", "download_id": None}),
        ("magneto://reveal", {"action": "reveal", "download_id": None}),
        ("magneto://unknown", None),
        ("magnet:?xt=urn:btih:abc", None),
        ("https://example.com", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_command_uri(uri, expected):
    assert parse_command_uri(uri) == expected


@pytest.mark.parametrize(
    ("kind", "download_id", "action", "expected"),
    [
        ("completed", 7, None, "magneto://reveal?download=7"),
        ("completed", None, None, None),
        ("failed", None, None, "magneto://open-downloads"),
        ("verification_failed", 7, None, "magneto://open-downloads"),
        ("info", None, "detected", "magneto://open-detected"),
        ("error", None, None, None),
    ],
)
def test_build_launch_uri(kind, download_id, action, expected):
    assert build_launch_uri(kind, download_id, action) == expected


def test_apply_command_open_detected_uses_window_hook():
    window = FakeWindow()
    assert apply_command(window, "magneto://open-detected") is True
    assert window.detected == [True]


def test_apply_command_open_detected_falls_back_without_hook():
    calls = []

    class MinimalWindow:
        def show(self):
            calls.append("show")

        def raise_(self):
            calls.append("raise")

        def activateWindow(self):
            calls.append("activate")

        def _activate_nav(self, key):
            calls.append(key)

    window = MinimalWindow()
    assert apply_command(window, "magneto://open-detected") is True
    assert calls == ["show", "raise", "activate", "detected"]


def test_apply_command_open_downloads():
    window = FakeWindow()
    assert apply_command(window, "magneto://open-downloads") is True
    assert window.navs == ["downloads"]


def test_apply_command_reveal_reveals_path(monkeypatch, tmp_path):
    from magnetoclip.ui import util as ui_util

    revealed = []
    monkeypatch.setattr(ui_util, "reveal_path", lambda path: revealed.append(path))
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"data")

    class FakeManager:
        def path_of(self, download_id):
            return target

    window = FakeWindow()
    window.context = type("Ctx", (), {"manager": FakeManager()})()
    assert apply_command(window, "magneto://reveal?download=5") is True
    assert window.navs == ["downloads"]
    assert revealed == [target]


def test_apply_command_unknown_uri_is_ignored():
    window = FakeWindow()
    assert apply_command(window, "magneto://nope") is False
    assert window.navs == []