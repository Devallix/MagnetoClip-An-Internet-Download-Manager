"""Proxy profile manager tests."""

from __future__ import annotations

import pytest

from magnetoclip.app.lifecycle import build_context
from magnetoclip.core.proxies.manager import ProxyManager
from magnetoclip.network.proxy.profiles import ProxySpec


def make_context(tmp_path):
    return build_context(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


def test_proxy_crud(tmp_path):
    context = make_context(tmp_path)
    manager: ProxyManager = context.proxies
    assert manager.list() == []

    profile = manager.add(
        "Corp", proxy_type="http", host="proxy.corp.example", port=8080
    )
    assert profile.id is not None
    assert manager.get(profile.id).name == "Corp"
    assert manager.get_by_name("Corp").host == "proxy.corp.example"

    with pytest.raises(ValueError):
        manager.add("Corp")

    manager.remove(profile.id)
    assert manager.list() == []
    with pytest.raises(KeyError):
        manager.remove(profile.id)
    context.database.close()


def test_proxy_spec_conversion(tmp_path):
    context = make_context(tmp_path)
    manager: ProxyManager = context.proxies
    profile = manager.add(
        "Auth", proxy_type="https", host="p.example", port=8443, username_ref="bob"
    )
    spec = manager.to_spec(profile.id)
    assert isinstance(spec, ProxySpec)
    assert spec.type == "https"
    assert spec.host == "p.example"
    assert spec.port == 8443
    assert spec.username == "bob"
    assert manager.to_spec(None) is None
    context.database.close()


def test_proxy_url_rendering():
    spec = ProxySpec(type="http", host="h.example", port=3128, username="u", password="p")
    assert spec.to_url() == "http://u:p@h.example:3128"
    assert ProxySpec(type="socks5", host="h.example").to_url() is None
    assert ProxySpec(type="direct").to_url() is None
