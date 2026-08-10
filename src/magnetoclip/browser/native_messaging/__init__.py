"""Native messaging support."""

from __future__ import annotations

from .host import run_host
from .protocol import decode_message, encode_message, read_message, write_message

__all__ = [
    "run_host",
    "decode_message",
    "encode_message",
    "read_message",
    "write_message",
]
