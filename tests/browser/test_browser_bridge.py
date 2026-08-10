"""Tests for the browser integration bridge and installer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.browser.integration.install import (
    build_host_manifest,
    ensure_extension_key,
    extension_id_from_public_key,
    host_manifest_path,
)
from magnetoclip.browser.manager import BrowserManager
from magnetoclip.core.events.bus import Events
from tests.support.http_server import PayloadServer


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    context.settings.set("browser.integration_enabled", True)
    return context


# ----- bridge -----


def test_ping_pong(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    assert bridge.handle_message({"type": "ping"}) == {"type": "pong"}
    asyncio.run(context.shutdown())


def test_unknown_type_errors(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "nope"})
    assert response["type"] == "error"
    asyncio.run(context.shutdown())


def test_capture_rejected_when_integration_disabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "capture", "url": "https://example.com/x"})
    assert response["type"] == "capture_error"
    asyncio.run(context.shutdown())


def test_settings_reports_flags(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.capture_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"type": "settings"})
    assert response["type"] == "settings_ok"
    assert response["integration_enabled"] is True
    assert response["capture_enabled"] is False
    asyncio.run(context.shutdown())


@pytest.mark.asyncio
async def test_capture_creates_and_starts_download(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    payload = b"x" * 4096
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    posted: list[dict] = []
    context.events.connect(Events.BROWSER_EVENT, posted.append)

    with PayloadServer(payload) as server:
        response = bridge.handle_message(
            {
                "type": "capture",
                "url": server.url,
                "filename": "captured.bin",
                "referrer": "https://example.com/page",
                "source": "extension",
            }
        )
        assert response["type"] == "capture_ok"
        download_id = response["download_id"]
        deadline = asyncio.get_running_loop().time() + 15
        status = None
        final_snapshot = None
        while asyncio.get_running_loop().time() < deadline:
            download = context.manager.get_download(download_id)
            if download is None:
                break
            final_snapshot = context.manager.snapshot_item(download)
            status = final_snapshot["status"]
            if status in ("completed", "failed", "verification_failed"):
                break
            await asyncio.sleep(0.02)
        assert status == "completed", f"expected completed, got {status}"
        assert Path(final_snapshot["save_path"]).exists()

    assert len(posted) == 1
    assert posted[0]["download_id"] == download_id
    await context.shutdown()


def test_capture_pending_when_confirm_enabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", True)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "id": 7,
            "type": "capture",
            "url": "https://example.com/file.zip",
            "filename": "file.zip",
            "source": "context_menu",
        }
    )
    assert response["type"] == "capture_pending"
    assert response["id"] == 7
    assert response["filename"] == "file.zip"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].url == "https://example.com/file.zip"
    assert pending[0].status == "pending"
    asyncio.run(context.shutdown())


def test_page_scan_capture_always_pending(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.confirm_capture", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {
            "type": "capture",
            "url": "https://x.com/user/status/123/video",
            "filename": "video.mp4",
            "referrer": "https://x.com/user/status/123",
            "source": "page_scan",
        }
    )
    assert response["type"] == "capture_pending"
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        pending = PendingCaptureRepository(session).pending()
    assert len(pending) == 1
    assert pending[0].source == "page_scan"
    asyncio.run(context.shutdown())


def test_capture_dedupes_pending(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    message = {"type": "capture", "url": "https://example.com/file.zip"}
    first = bridge.handle_message(message)
    second = bridge.handle_message(message)
    assert first["type"] == "capture_pending"
    assert second["type"] == "capture_pending"
    assert first["id"] == second["id"]
    with context.session_factory() as session:
        from magnetoclip.database.repositories import PendingCaptureRepository

        assert len(PendingCaptureRepository(session).pending()) == 1
    asyncio.run(context.shutdown())


def test_page_scan_records_detection(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    posted: list[dict] = []
    context.events.connect(Events.BROWSER_EVENT, posted.append)
    response = bridge.handle_message(
        {
            "type": "page_scan",
            "url": "https://example.com/page",
            "files": [
                {"url": "https://example.com/a.zip", "filename": "a.zip", "detected_type": "archive"},
                {"url": "https://example.com/b.pdf", "filename": "b.pdf", "detected_type": "document"},
            ],
        }
    )
    assert response["type"] == "page_scan_ok"
    assert response["count"] == 2
    with context.session_factory() as session:
        from magnetoclip.database.repositories import BrowserDetectionRepository

        detections = BrowserDetectionRepository(session).unnotified()
    assert len(detections) == 1
    assert detections[0].page_url == "https://example.com/page"
    assert detections[0].count == 2
    assert posted and posted[0]["source"] == "page_scan"
    asyncio.run(context.shutdown())


def test_page_scan_rejected_when_integration_disabled(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", False)
    bridge = BrowserManager(context)
    response = bridge.handle_message(
        {"type": "page_scan", "url": "https://example.com/page", "files": []}
    )
    assert response["type"] == "page_scan_error"
    asyncio.run(context.shutdown())


def test_request_id_is_echoed(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    response = bridge.handle_message({"id": 42, "type": "ping"})
    assert response == {"id": 42, "type": "pong"}
    asyncio.run(context.shutdown())


@pytest.mark.asyncio
async def test_capture_rejects_bad_urls(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    for bad in ("", "not a url", "ftp://example.com/x", "file:///etc/passwd"):
        response = bridge.handle_message({"type": "capture", "url": bad})
        assert response["type"] == "capture_error", bad
    await context.shutdown()


@pytest.mark.asyncio
async def test_status_reports_counts(tmp_path):
    context = make_context(tmp_path)
    bridge = BrowserManager(context)
    bridge.start(asyncio.get_running_loop())
    response = bridge.handle_message({"type": "status"})
    assert response["type"] == "status_ok"
    assert response["active"] == 0
    assert response["completed"] == 0
    assert response["integration_enabled"] is True

    with PayloadServer(b"y" * 2048) as server:
        download = context.manager.add(server.url, filename="one.bin")
        context.manager.start(download.id)
        await asyncio.sleep(0.1)
        response = bridge.handle_message({"type": "status"})
        assert response["active"] == 1 or response["completed"] == 1
    await context.shutdown()


# ----- installer -----


def test_extension_id_stable_and_valid(tmp_path):
    key_path, public_key = ensure_extension_key(tmp_path)
    assert key_path.exists()
    first = extension_id_from_public_key(public_key)
    second = extension_id_from_public_key(public_key)
    assert first == second
    assert len(first) == 32
    assert all(c in "abcdefghijklmnop" for c in first)


def test_host_manifest_chromium():
    import json as jsonlib

    from magnetoclip.browser.integration.install import build_host_manifest

    manifest = build_host_manifest(
        host_path="C:/app/magnetoclip.exe", extension_id="a" * 32, browser="chrome", data_dir=None
    )
    assert manifest["name"] == "com.magnetoclip.host"
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == ["chrome-extension://" + "a" * 32 + "/"]
    assert jsonlib.dumps(manifest)


def test_host_manifest_firefox():
    manifest = build_host_manifest(
        host_path="C:/app/magnetoclip.exe", extension_id="a" * 32, browser="firefox", data_dir=None
    )
    assert manifest["allowed_extensions"] == ["magneto-companion@magnetoclip.app"]
    assert "allowed_origins" not in manifest


def test_host_manifest_path_per_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    chrome = host_manifest_path("chrome", tmp_path)
    assert str(chrome).endswith(
        "Local\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host.json"
    )
    firefox = host_manifest_path("firefox", tmp_path)
    assert str(firefox).endswith(
        "Roaming\\Mozilla\\NativeMessagingHosts\\com.magnetoclip.host.json"
    )


def test_native_hosts_registry_key_per_browser():
    import importlib

    install_mod = importlib.import_module("magnetoclip.browser.integration.install")

    assert (
        install_mod._native_hosts_registry_key("chrome")
        == "Software\\Google\\Chrome\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("edge")
        == "Software\\Microsoft\\Edge\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("chromium")
        == "Software\\Chromium\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("brave")
        == "Software\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts"
    )
    assert (
        install_mod._native_hosts_registry_key("vivaldi")
        == "Software\\Vivaldi\\NativeMessagingHosts"
    )
    assert install_mod._native_hosts_registry_key("firefox") is None


def test_register_and_unregister_native_host(monkeypatch, tmp_path):
    import importlib

    install_mod = importlib.import_module("magnetoclip.browser.integration.install")

    created: list[tuple[str, str]] = []
    values: list[tuple[str, str, str, str]] = []
    deleted: list[tuple[str, str]] = []

    class FakeKey:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeWinreg:
        HKEY_CURRENT_USER = "HKCU"
        REG_SZ = "REG_SZ"
        KEY_SET_VALUE = 0x0002

        @staticmethod
        def CreateKey(hive, key):
            created.append((hive, key))
            return FakeKey(key)

        @staticmethod
        def SetValue(key, name, value_type, value):
            values.append((key.name, name, value_type, value))

        @staticmethod
        def OpenKey(hive, key, reserved, access):
            return FakeKey(key)

        @staticmethod
        def DeleteKey(key, subkey):
            deleted.append((key.name, subkey))

    monkeypatch.setattr(install_mod, "winreg", FakeWinreg)

    manifest = tmp_path / "com.magnetoclip.host.json"
    install_mod._register_native_host("chrome", manifest)
    assert created == [
        ("HKCU", "Software\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host")
    ]
    assert values == [
        (
            "Software\\Google\\Chrome\\NativeMessagingHosts\\com.magnetoclip.host",
            "",
            "REG_SZ",
            str(manifest),
        )
    ]

    install_mod._register_native_host("firefox", manifest)
    assert len(created) == 1

    install_mod._unregister_native_host("chrome")
    assert deleted == [
        ("Software\\Google\\Chrome\\NativeMessagingHosts", "com.magnetoclip.host")
    ]


def test_registry_noop_when_winreg_unavailable(monkeypatch, tmp_path):
    import importlib

    install_mod = importlib.import_module("magnetoclip.browser.integration.install")

    monkeypatch.setattr(install_mod, "winreg", None)
    install_mod._register_native_host("chrome", tmp_path / "x.json")
    install_mod._unregister_native_host("chrome")
