"""Manager-level wiring of proxy/auth/cookies into download specs."""

from __future__ import annotations

import asyncio

from magnetoclip.app.lifecycle import build_context
from magnetoclip.database.models import Download

from tests.support.http_server import PayloadServer


def make_context(tmp_path):
    context = build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    context.settings.set("downloads.default_directory", str(tmp_path / "downloads"))
    return context


def test_add_persists_proxy_auth_cookies(tmp_path):
    context = make_context(tmp_path)
    proxy = context.proxies.add("Corp", proxy_type="http", host="p.example", port=8080)

    download = context.manager.add(
        "https://example.com/file.zip",
        filename="file.zip",
        proxy_profile_id=proxy.id,
        auth_username="bob",
        auth_password="secret",
        cookies={"session": "abc"},
    )
    with context.session_factory() as session:
        loaded = session.get(Download, download.id)
        assert loaded.proxy_profile_id == proxy.id
        assert loaded.auth_ref == "bob"
        assert "session=abc" in loaded.headers_json["cookie"]
    context.database.close()


def test_add_uses_default_proxy(tmp_path):
    context = make_context(tmp_path)
    proxy = context.proxies.add("Default", proxy_type="http", host="d.example", port=80)
    context.settings.set("network.default_proxy_id", proxy.id)

    download = context.manager.add("https://example.com/file.zip", filename="f.zip")
    with context.session_factory() as session:
        loaded = session.get(Download, download.id)
        assert loaded.proxy_profile_id == proxy.id
    context.database.close()


def test_build_spec_wires_proxy_auth_cookies(tmp_path):
    context = make_context(tmp_path)
    proxy = context.proxies.add("Corp", proxy_type="http", host="p.example", port=8080)
    download = context.manager.add(
        "https://example.com/file.zip",
        filename="file.zip",
        proxy_profile_id=proxy.id,
        cookies={"a": "1"},
    )
    spec = context.manager._build_spec(download)
    assert spec.proxy.host == "p.example"
    assert spec.cookies == {"a": "1"}
    assert "cookie" not in spec.headers
    context.database.close()


def test_build_spec_plain_no_special_config(tmp_path):
    context = make_context(tmp_path)
    download = context.manager.add("https://example.com/file.zip", filename="plain.zip")
    spec = context.manager._build_spec(download)
    assert spec.proxy is None
    assert spec.cookies == {}
    assert spec.auth is None
    context.database.close()
