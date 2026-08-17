"""Test support: a threaded HTTP server with Range support and failure modes."""

from __future__ import annotations

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

SERVED_PATH = "/file.bin"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: ANN002 - silence
        pass

    def do_HEAD(self) -> None:
        self._serve()

    def do_GET(self) -> None:
        self._serve()

    def _serve(self) -> None:
        server = self.server
        server.requests_count[0] += 1

        if self.path != SERVED_PATH:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if server.fail_times[0] > 0:
            server.fail_times[0] -= 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        payload = server.payload
        range_header = self.headers.get("Range")
        if range_header:
            server.range_requests_count[0] += 1
            match = _RANGE_RE.search(range_header)
            start = int(match.group(1)) if match and match.group(1) else 0
            end = (
                int(match.group(2))
                if match and match.group(2)
                else len(payload) - 1
            )
            end = min(end, len(payload) - 1)
            if start > end or start >= len(payload):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(payload)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = payload[start : end + 1]
            status = 206
            content_range = f"bytes {start}-{end}/{len(payload)}"
        else:
            body = payload
            status = 200
            content_range = None

        self.send_response(status)
        self.send_header("Content-Type", server.content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"test-etag"')
        self.send_header("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if self.command == "HEAD":
            return

        disconnect_after = server.disconnect_after_bytes[0]
        if disconnect_after is not None and disconnect_after < len(body):
            self.wfile.write(body[:disconnect_after])
            self.wfile.flush()
            self.connection.close()
            return
        chunk_size = server.chunk_size[0]
        if chunk_size and len(body) > chunk_size:
            for offset in range(0, len(body), chunk_size):
                self.wfile.write(body[offset : offset + chunk_size])
                self.wfile.flush()
                if server.chunk_delay[0]:
                    time.sleep(server.chunk_delay[0])
            return
        self.wfile.write(body)


class _NoRangeHandler(_Handler):
    """Always serves the full body with 200, ignoring Range requests."""

    def _serve(self) -> None:
        server = self.server
        server.requests_count[0] += 1

        if self.path != SERVED_PATH:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if server.fail_times[0] > 0:
            server.fail_times[0] -= 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = server.payload
        self.send_response(200)
        self.send_header("Content-Type", server.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command == "HEAD":
            return
        self.wfile.write(payload)


class PayloadServer:
    """Context manager exposing a Range-capable HTTP server on localhost."""

    def __init__(
        self,
        payload: bytes,
        *,
        fail_times: int = 0,
        disconnect_after_bytes: int | None = None,
        chunk_size: int = 0,
        chunk_delay: float = 0.0,
        content_type: str = "application/octet-stream",
        handler_class=_Handler,
    ) -> None:
        self.payload = payload
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        self._httpd.payload = payload
        self._httpd.content_type = content_type
        self._httpd.requests_count = [0]
        self._httpd.range_requests_count = [0]
        self._httpd.fail_times = [fail_times]
        self._httpd.disconnect_after_bytes = [disconnect_after_bytes]
        self._httpd.chunk_size = [chunk_size]
        self._httpd.chunk_delay = [chunk_delay]
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        return f"{self.base}/file.bin"

    @property
    def requests(self) -> int:
        return self._httpd.requests_count[0]

    @property
    def range_requests(self) -> int:
        return self._httpd.range_requests_count[0]

    def __enter__(self) -> PayloadServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:  # noqa: ANN002
        self._httpd.shutdown()
        self._httpd.server_close()


def no_range_server(payload: bytes, **kwargs) -> PayloadServer:
    """A server that returns 200 and ignores Range requests."""
    return PayloadServer(payload, handler_class=_NoRangeHandler, **kwargs)


def html_server(payload: bytes, **kwargs) -> PayloadServer:
    """A Range-capable server that serves its payload as an HTML document."""
    return PayloadServer(payload, content_type="text/html", **kwargs)
