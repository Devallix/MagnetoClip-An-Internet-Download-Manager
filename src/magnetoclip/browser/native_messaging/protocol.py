"""Native messaging wire protocol (length-prefixed JSON, as per Chrome/Firefox)."""

from __future__ import annotations

import io
import json
import struct
from typing import BinaryIO

_HEADER = struct.Struct("<I")


def read_message(stream: BinaryIO) -> dict | None:
    """Read one message; returns None at end of stream or on truncation."""
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) < _HEADER.size:
        return None
    (length,) = _HEADER.unpack(header)
    if length > 16 * 1024 * 1024:
        raise ValueError("native message too large")
    payload = stream.read(length)
    if len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def write_message(stream: BinaryIO, message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def encode_message(message: dict) -> bytes:
    """Serialize one message into the wire format (for tests)."""
    buffer = io.BytesIO()
    write_message(buffer, message)
    return buffer.getvalue()


def decode_message(raw: bytes) -> dict:
    """Parse a single wire-format message (for tests)."""
    return read_message(io.BytesIO(raw))
