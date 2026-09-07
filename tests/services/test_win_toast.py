"""Tests for the WinRT toast bridge."""

from __future__ import annotations

import sys
import types

import pytest

from magnetoclip.services.notification import win_toast

POLICIES_WIN = sys.platform == "win32"


def _fake_run():
    def _run(argv, **kwargs):
        assert argv[0] == "powershell"
        assert "-EncodedCommand" in argv
        return types.SimpleNamespace(returncode=0, stderr=b"")

    return _run


def test_build_xml_escapes_and_includes_protocol_action():
    xml = win_toast._build_xml("A & B", 'C < "D"', "magneto://reveal?download=7")
    assert "A &amp; B" in xml
    assert "C &lt;" in xml and "&quot;D&quot;" in xml
    assert 'launch="magneto://reveal?download=7"' in xml
    assert 'activationType="protocol"' in xml
    assert 'protocol="magneto://reveal?download=7"' in xml


def test_build_xml_omits_actions_without_launch():
    xml = win_toast._build_xml("Title", "Body", None)
    assert "<actions>" not in xml
    assert "launch=" not in xml


def test_encode_command_is_utf16le():
    encoded = win_toast._encode_command("hi")
    assert encoded == "aABpAA=="


def test_show_toast_success(monkeypatch):
    received = []
    monkeypatch.setattr(win_toast.subprocess, "run", lambda argv, **kw: received.append(argv) or _fake_run()(argv, **kw))
    assert win_toast.show_toast("Title", "Body", launch="magneto://open") is True
    assert received and "-EncodedCommand" in received[0]


def test_show_toast_failure(monkeypatch):
    monkeypatch.setattr(
        win_toast.subprocess,
        "run",
        lambda argv, **kw: types.SimpleNamespace(
            returncode=1, stderr=b"boom"
        ),
    )
    assert win_toast.show_toast("Title", "Body") is False


def test_show_toast_not_windows(monkeypatch):
    called = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        win_toast.subprocess,
        "run",
        lambda argv, **kw: called.append(argv) or types.SimpleNamespace(returncode=0, stderr=b""),
    )
    assert win_toast.show_toast("Title", "Body") is False
    assert called == []