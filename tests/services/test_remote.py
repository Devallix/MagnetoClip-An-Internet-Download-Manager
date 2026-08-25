"""Tests for the Remote Control LAN dashboard server."""

from __future__ import annotations

import re
import socket

import pytest
from aiohttp.test_utils import TestClient, TestServer

from magnetoclip.config.settings import Settings
from magnetoclip.services.remote.server import (
    RemoteServer,
    build_app,
    generate_token,
    lan_ip,
)

TOKEN = "unit-test-token"


class _StubDownload:
    def __init__(self, download_id: int, url: str) -> None:
        self.id = download_id
        self.url = url


class StubManager:
    """Manager double covering the surface the remote API touches."""

    def __init__(self) -> None:
        self._downloads: dict[int, _StubDownload] = {}
        self._next_id = 1
        self.remove_calls: list[tuple[int, dict]] = []

    def add(self, url: str, **_: object) -> _StubDownload:
        if not isinstance(url, str) or not url.lower().startswith(
            ("http://", "https://", "magnet:")
        ):
            raise ValueError("invalid URL")
        download = _StubDownload(self._next_id, url)
        self._next_id += 1
        self._downloads[download.id] = download
        return download

    def get_download(self, download_id: int) -> _StubDownload | None:
        return self._downloads.get(download_id)

    def start(self, download_id: int, **_: object) -> bool:
        return download_id in self._downloads

    def pause(self, download_id: int) -> None:
        assert download_id in self._downloads

    def resume(self, download_id: int) -> None:
        assert download_id in self._downloads

    def remove(self, download_id: int, *, delete_file: bool = False) -> None:
        self.remove_calls.append((download_id, {"delete_file": delete_file}))
        self._downloads.pop(download_id, None)

    def list_snapshots(self, **_: object) -> list[dict]:
        return [
            {
                "id": d.id,
                "url": d.url,
                "filename": f"file-{d.id}.zip",
                "status": "queued",
                "speed": 0.0,
                "size_total": 1024,
                "size_downloaded": 0,
                "eta_seconds": None,
            }
            for d in sorted(self._downloads.values(), key=lambda x: x.id)
        ]


@pytest.fixture
def context():
    class _Context:
        pass

    ctx = _Context()
    ctx.settings = Settings(
        {"remote.enabled": True, "remote.port": 8477, "remote.token": TOKEN}
    )
    ctx.manager = StubManager()
    return ctx


@pytest.fixture
async def client(context):
    app = build_app(context)
    async with TestClient(TestServer(app)) as client:
        yield client, context


AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TestAuth:
    async def test_missing_token_is_unauthorized(self, client) -> None:
        http, _ = client
        response = await http.get("/api/downloads")
        assert response.status == 401

    async def test_wrong_token_is_unauthorized(self, client) -> None:
        http, _ = client
        response = await http.get(
            "/api/downloads", headers={"Authorization": "Bearer nope"}
        )
        assert response.status == 401

    async def test_query_param_token_accepted(self, client) -> None:
        http, _ = client
        response = await http.get(f"/api/downloads?token={TOKEN}")
        assert response.status == 200

    async def test_bearer_header_accepted(self, client) -> None:
        http, _ = client
        response = await http.get("/api/downloads", headers=AUTH)
        assert response.status == 200

    async def test_dashboard_page_needs_no_token(self, client) -> None:
        http, _ = client
        response = await http.get("/")
        assert response.status == 200
        body = await response.text()
        assert "MagnetoClip" in body

    async def test_app_icon_served_without_token(self, client) -> None:
        http, _ = client
        response = await http.get("/icon.png")
        assert response.status == 200
        assert response.content_type == "image/png"
        assert (await response.read())[:8] == b"\x89PNG\r\n\x1a\n"


class TestDownloadsApi:
    async def test_list_shape(self, client) -> None:
        http, context = client
        context.manager.add("https://example.com/a.zip")
        response = await http.get("/api/downloads", headers=AUTH)
        assert response.status == 200
        payload = await response.json()
        assert payload["active_count"] == 0
        assert payload["total_speed"] == 0.0
        assert len(payload["downloads"]) == 1
        item = payload["downloads"][0]
        for key in ("id", "url", "filename", "status", "speed", "eta_seconds"):
            assert key in item
        assert item["filename"] == "file-1.zip"

    async def test_add_then_pause_resume(self, client) -> None:
        http, context = client
        response = await http.post(
            "/api/add",
            headers={**AUTH, "Content-Type": "application/json"},
            json={"url": "https://example.com/video.mp4"},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["ok"] is True
        assert payload["started"] is True
        download_id = payload["id"]

        paused = await http.post(f"/api/downloads/{download_id}/pause", headers=AUTH)
        resumed = await http.post(f"/api/downloads/{download_id}/resume", headers=AUTH)
        assert paused.status == 200 and (await paused.json())["ok"] is True
        assert resumed.status == 200 and (await resumed.json())["ok"] is True

    async def test_malformed_add_rejected(self, client) -> None:
        http, _ = client
        empty = await http.post(
            "/api/add",
            headers={**AUTH, "Content-Type": "application/json"},
            json={"nope": True},
        )
        assert empty.status == 400
        bad_url = await http.post(
            "/api/add",
            headers={**AUTH, "Content-Type": "application/json"},
            json={"url": "ftp://not-supported"},
        )
        assert bad_url.status == 400
        raw = await http.post("/api/add", headers=AUTH, data="not-json")
        assert raw.status == 400

    async def test_remove_keeps_file(self, client) -> None:
        http, context = client
        download = context.manager.add("https://example.com/b.zip")
        response = await http.post(
            f"/api/downloads/{download.id}/remove", headers=AUTH
        )
        assert response.status == 200
        assert context.manager.remove_calls == [(download.id, {"delete_file": False})]

    async def test_retry_maps_to_start(self, client) -> None:
        http, context = client
        download = context.manager.add("https://example.com/c.zip")
        response = await http.post(
            f"/api/downloads/{download.id}/retry", headers=AUTH
        )
        assert response.status == 200
        assert (await response.json())["ok"] is True

    async def test_unknown_action_and_missing_download(self, client) -> None:
        http, context = client
        download = context.manager.add("https://example.com/d.zip")
        unknown = await http.post(
            f"/api/downloads/{download.id}/explode", headers=AUTH
        )
        assert unknown.status == 400
        missing = await http.post("/api/downloads/999/pause", headers=AUTH)
        assert missing.status == 404


class TestPairingHelpers:
    def test_generate_token_urlsafe_length(self) -> None:
        token = generate_token()
        assert 20 <= len(token) <= 48
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", token)

    def test_pair_url_contains_fragment(self, context) -> None:
        server = RemoteServer(context)
        url = server.pair_url()
        assert url.startswith("http://")
        assert ":8477/#pair=" in url
        assert url.endswith(TOKEN)

    def test_lan_ip_resolves(self) -> None:
        address = lan_ip()
        socket.inet_pton(socket.AF_INET, address)
