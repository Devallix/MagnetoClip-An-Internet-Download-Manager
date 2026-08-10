"""A server that echoes request headers so clients can be verified."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: ANN002
        pass

    def do_GET(self) -> None:
        body = json.dumps(
            {
                "path": self.path,
                "cookie": self.headers.get("Cookie", ""),
                "authorization": self.headers.get("Authorization", ""),
                "user_agent": self.headers.get("User-Agent", ""),
                "custom": self.headers.get("X-Custom", ""),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EchoServer:
    """Returns JSON describing the last request's headers."""

    def __init__(self) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        return f"{self.base}/check"

    def __enter__(self) -> EchoServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:  # noqa: ANN002
        self._httpd.shutdown()
        self._httpd.server_close()
