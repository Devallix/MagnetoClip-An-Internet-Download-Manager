"""Remote Control service: LAN dashboard server for phone/browser clients."""

from __future__ import annotations

from .server import RemoteServer, ensure_server, generate_token, lan_ip

__all__ = ["RemoteServer", "ensure_server", "generate_token", "lan_ip"]
