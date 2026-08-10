"""End-to-end tests for proxy, auth, and cookies through the engine."""

from __future__ import annotations

import asyncio

import pytest

from magnetoclip.engine.downloader.engine import MagnetoCore, spec_from_url
from magnetoclip.network.auth.credentials import AuthSpec
from magnetoclip.network.http.client import build_client
from magnetoclip.network.proxy.profiles import ProxySpec

from tests.support.echo_server import EchoServer
from tests.support.http_server import PayloadServer

PAYLOAD = bytes(range(256)) * 4096


def _make_spec(server, tmp_path, **overrides):
    defaults = dict(
        download_id=7,
        url=server.url,
        save_dir=tmp_path,
        filename="probe.bin",
        connections_max=1,
        retry_max=2,
        retry_base=0.01,
    )
    defaults.update(overrides)
    return spec_from_url(**defaults)


@pytest.mark.asyncio
async def test_cookies_are_sent_per_request(tmp_path):
    with EchoServer() as server:
        spec = _make_spec(
            server, tmp_path, cookies={"session": "topsecret", "lang": "en"}
        )
        core = MagnetoCore()
        try:
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            echo = spec.final_path.read_text()
            assert "session=topsecret" in echo
            assert "lang=en" in echo
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_basic_auth_header_sent(tmp_path):
    with EchoServer() as server:
        spec = _make_spec(
            server,
            tmp_path,
            auth=AuthSpec(type="basic", username="user", password="pass"),
        )
        core = MagnetoCore()
        try:
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            echo = spec.final_path.read_text()
            assert "Basic " in echo
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_bearer_token_header_sent(tmp_path):
    with EchoServer() as server:
        spec = _make_spec(
            server,
            tmp_path,
            auth=AuthSpec(type="bearer", token="tok123"),
        )
        core = MagnetoCore()
        try:
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            echo = spec.final_path.read_text()
            assert "Bearer tok123" in echo
        finally:
            await core.shutdown()


@pytest.mark.asyncio
async def test_extra_headers_and_user_agent(tmp_path):
    with EchoServer() as server:
        spec = _make_spec(
            server,
            tmp_path,
            headers={"X-Custom": "yes"},
            user_agent="MagnetoClip-Test/1.0",
        )
        core = MagnetoCore()
        try:
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            echo = spec.final_path.read_text()
            assert '"X-Custom": "yes"' in echo or "yes" in echo
            assert "MagnetoClip-Test/1.0" in echo
        finally:
            await core.shutdown()


def test_shared_client_used_without_special_config():
    from pathlib import Path

    core = MagnetoCore()
    spec = spec_from_url(
        download_id=1,
        url="https://example.com/x",
        save_dir=Path("."),
        filename="x.bin",
    )
    client, close = core._resolve_client(spec)
    assert client is core._client
    assert close is False
    asyncio.run(core.shutdown())


def test_per_spec_client_for_proxy_auth_cookies(tmp_path):
    core = MagnetoCore()
    try:
        for kwargs in (
            {"proxy": ProxySpec(type="http", host="p.example", port=8080)},
            {"auth": AuthSpec(type="basic", username="u")},
            {"cookies": {"a": "1"}},
        ):
            spec = spec_from_url(
                download_id=1,
                url="https://example.com/x",
                save_dir=tmp_path,
                filename="x.bin",
                **kwargs,
            )
            client, close = core._resolve_client(spec)
            assert client is not core._client
            assert close is True
    finally:
        asyncio.run(core.shutdown())


@pytest.mark.asyncio
async def test_download_with_cookies_matches_payload(tmp_path):
    with PayloadServer(PAYLOAD) as server:
        spec = _make_spec(server, tmp_path, cookies={"a": "1"}, connections_max=4)
        core = MagnetoCore()
        try:
            task = core.submit(spec)
            result = await task.run()
            assert result == "completed"
            assert spec.final_path.read_bytes() == PAYLOAD
        finally:
            await core.shutdown()


def test_build_client_includes_cookies():
    import asyncio as aio

    async def _probe():
        client = build_client(cookies={"x": "y"})
        try:
            assert "x=y" in str(client.cookies)
        finally:
            await client.aclose()

    aio.run(_probe())
