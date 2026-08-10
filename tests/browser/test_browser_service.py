"""Tests for the browser integration service (launcher, detection, installs)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.browser.integration.install import host_manifest_path
from magnetoclip.browser.service import BrowserIntegrationService, _expand_env
from magnetoclip.resources import resource_path


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    return context


def test_expand_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert Path(_expand_env("{LOCALAPPDATA}\\x")) == tmp_path / "x"


def test_host_command_includes_browser_flag(tmp_path):
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    command = service.host_command()
    assert "--browser-host" in command
    assert Path(command[0]).is_file()
    asyncio.run(context.shutdown())


def test_ensure_launcher_writes_batch_file(tmp_path):
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    path = service.ensure_launcher()
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert content.startswith("@echo off")
    assert "--browser-host" in content
    asyncio.run(context.shutdown())


def test_detect_browsers_via_env(tmp_path, monkeypatch):
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"MZ")
    firefox = tmp_path / "Mozilla Firefox" / "firefox.exe"
    firefox.parent.mkdir(parents=True)
    firefox.write_bytes(b"MZ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))

    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    detected = service.detect_browsers()
    assert "chrome" in detected
    assert "firefox" in detected
    assert set(detected) <= set(("chrome", "edge", "firefox", "brave", "vivaldi", "chromium"))
    asyncio.run(context.shutdown())


def test_install_and_uninstall_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    manifest = host_manifest_path("chrome", context.data_dir)

    results = service.install(["chrome"])
    assert results["chrome"].startswith("registered")
    assert manifest.exists()
    content = json.loads(manifest.read_text(encoding="utf-8"))
    assert content["name"] == "com.magnetoclip.host"
    assert content["path"] == str(service.host_launcher_path())
    assert content["allowed_origins"] == [f"chrome-extension://{service.extension_id()}/"]
    assert service.host_launcher_path().exists()

    service.uninstall(["chrome"])
    assert not manifest.exists()
    asyncio.run(context.shutdown())


def test_install_extension_copies_with_key(tmp_path):
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    extension_dir = service.install_extension()
    assert (extension_dir / "manifest.json").exists()
    assert (extension_dir / "background.js").exists()
    manifest = json.loads((extension_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["key"]) > 0
    assert len(service.extension_id()) == 32
    assert service.status()["extension_ready"] is True
    asyncio.run(context.shutdown())


def test_ensure_installed_posts_status(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    context = make_context(tmp_path)
    posted: list[dict] = []
    context.events.connect("browser.status_changed", posted.append)
    service = BrowserIntegrationService(context)
    service.ensure_installed()
    assert posted
    assert posted[-1]["launcher_exists"] is True
    assert posted[-1]["extension_ready"] is True
    asyncio.run(context.shutdown())


def test_status_reports_state(tmp_path):
    context = make_context(tmp_path)
    context.settings.set("browser.integration_enabled", True)
    service = BrowserIntegrationService(context)
    status = service.status()
    assert status["enabled"] is True
    assert status["extension_dir"].endswith("browser_extension")
    assert status["extension_id"] == service.extension_id()
    assert set(status["browsers"]) == {
        "chrome", "edge", "firefox", "brave", "vivaldi", "chromium"
    }
    assert "force_installed" in status["browsers"]["chrome"]
    asyncio.run(context.shutdown())


def test_force_install_unsupported_browser(tmp_path):
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    result = service.force_install("firefox")
    assert result["ok"] is False
    asyncio.run(context.shutdown())


def test_force_install_writes_policy_entry(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    calls: list[tuple] = []

    def fake_force(root, browser, extension_id, url):
        calls.append((browser, extension_id, url))

    monkeypatch.setattr("magnetoclip.browser.service._force_install_policy", fake_force)
    monkeypatch.setattr("magnetoclip.browser.service._policy_installed", lambda *_: True)
    result = service.force_install("chrome")
    assert result["ok"] is True
    browser, extension_id, url = calls[0]
    assert browser == "chrome"
    assert extension_id == service.extension_id()
    assert url == "https://clients2.google.com/service/update2/crx"
    asyncio.run(context.shutdown())


def test_force_install_falls_back_to_elevated(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)

    def raise_oserror(*_args):
        raise OSError("denied")

    monkeypatch.setattr("magnetoclip.browser.service._force_install_policy", raise_oserror)
    monkeypatch.setattr(
        "magnetoclip.browser.service._run_elevated_policy_script",
        lambda *_: True,
    )
    result = service.force_install("edge")
    assert result["ok"] is True
    assert "all users" in result["message"]
    asyncio.run(context.shutdown())


def test_remove_force_install_unsupported(tmp_path):
    context = make_context(tmp_path)
    service = BrowserIntegrationService(context)
    result = service.remove_force_install("firefox")
    assert result["ok"] is False
    asyncio.run(context.shutdown())
