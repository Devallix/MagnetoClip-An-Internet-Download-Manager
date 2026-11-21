"""Remote Control v1: aiohttp server exposing download controls on the LAN.

Runs natively on the application's qasync event loop. Every ``/api/*``
request must present the pairing token (``Authorization: Bearer`` header or
``?token=`` query parameter); the dashboard page itself is served without
auth so the phone can load it, and reads the token from the URL fragment
(``#pair=``) which browsers never send to the server.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import socket
from typing import Any

from aiohttp import web

from ...resources import resource_path
from ...services.logging import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 8477

CONTEXT_KEY = web.AppKey("context", object)
SETTINGS_KEY = web.AppKey("settings", object)


def generate_token() -> str:
    """Random pairing token, URL-safe and ~32 chars long."""
    return secrets.token_urlsafe(24)


def lan_ip() -> str:
    """Best-effort LAN address of this machine (no traffic is actually sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        probe.close()


def _token_from_request(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query.get("token")


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        expected = request.app[SETTINGS_KEY].get("remote.token", "")
        provided = _token_from_request(request) or ""
        if not expected or not hmac.compare_digest(expected, provided):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def build_app(context: Any) -> web.Application:
    """Build the aiohttp application for the remote dashboard."""
    app = web.Application(middlewares=[_auth_middleware])
    app[CONTEXT_KEY] = context
    app[SETTINGS_KEY] = context.settings
    manager = context.manager

    async def index(_request: web.Request) -> web.Response:
        path = resource_path("remote", "index.html")
        try:
            html = path.read_text(encoding="utf-8")
        except OSError:
            return web.Response(status=404, text="dashboard not found")
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def icon(_request: web.Request) -> web.Response:
        path = resource_path("icons", "logo.png")
        try:
            data = path.read_bytes()
        except OSError:
            raise web.HTTPNotFound(text="icon not found")
        return web.Response(
            body=data,
            content_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def downloads(_request: web.Request) -> web.Response:
        items = manager.list_snapshots(limit=2000)
        active = [
            item
            for item in items
            if item.get("status")
            in ("connecting", "downloading", "retrying", "verifying")
        ]
        return web.json_response(
            {
                "downloads": items,
                "active_count": len(active),
                "total_speed": sum(float(item.get("speed") or 0.0) for item in active),
            }
        )

    async def add(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON body
            return web.json_response({"error": "invalid JSON body"}, status=400)
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str) or not url.strip():
            return web.json_response({"error": "missing 'url'"}, status=400)
        try:
            download = manager.add(url.strip())
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        started = False
        try:
            started = bool(manager.start(download.id))
        except Exception as exc:  # noqa: BLE001 - auto-start is best effort
            log.warning("remote_auto_start_failed", id=download.id, error=str(exc))
        return web.json_response({"ok": True, "id": download.id, "started": started})

    async def action(request: web.Request) -> web.Response:
        download_id = int(request.match_info["download_id"])
        name = request.match_info["action"]
        if manager.get_download(download_id) is None:
            return web.json_response({"error": "not found"}, status=404)
        ok = False
        if name == "pause":
            manager.pause(download_id)
            ok = True
        elif name == "resume":
            manager.resume(download_id)
            ok = True
        elif name in ("start", "retry"):
            # retry reuses start(): failed downloads restart from the engine.
            ok = bool(manager.start(download_id))
        elif name == "remove":
            manager.remove(download_id, delete_file=False)
            ok = True
        else:
            return web.json_response({"error": f"unknown action '{name}'"}, status=400)
        return web.json_response({"ok": ok})

    app.router.add_get("/", index)
    app.router.add_get("/icon.png", icon)
    app.router.add_get("/api/downloads", downloads)
    app.router.add_post("/api/add", add)
    app.router.add_post("/api/downloads/{download_id}/{action}", action)
    return app


class RemoteServer:
    """Lifecycle wrapper around the aiohttp runner/site pair."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self._runner: web.AppRunner | None = None
        self.port = int(context.settings.get("remote.port", DEFAULT_PORT))

    @property
    def running(self) -> bool:
        return self._runner is not None

    def pair_url(self) -> str:
        token = self.context.settings.get("remote.token", "")
        return f"http://{lan_ip()}:{self.port}/#pair={token}"

    async def start(self) -> bool:
        """Bind 0.0.0.0:<port>; returns False when the port is unavailable."""
        if self.running:
            return True
        app = build_app(self.context)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", self.port)
            await site.start()
        except OSError as exc:
            log.warning("remote_server_bind_failed", port=self.port, error=str(exc))
            await runner.cleanup()
            return False
        self._runner = runner
        log.info("remote_server_started", port=self.port)
        return True

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is None:
            return
        try:
            await runner.cleanup()
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            raise
        except Exception as exc:  # noqa: BLE001 - shutdown must never raise
            log.warning("remote_server_stop_failed", error=str(exc))
        log.info("remote_server_stopped", port=self.port)


async def ensure_server(context: Any) -> RemoteServer | None:
    """Create/start the context's remote server when the feature is enabled.

    Safe to call repeatedly; returns ``None`` when disabled or the bind fails.
    """
    if not bool(context.settings.get("remote.enabled", False)):
        return None
    server = getattr(context, "remote", None)
    if server is None:
        server = RemoteServer(context)
        context.remote = server
    if not server.running and not await server.start():
        return None
    return server
