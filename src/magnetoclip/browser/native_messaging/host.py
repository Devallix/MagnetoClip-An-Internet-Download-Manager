"""Native messaging host process: read requests, write responses."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import BinaryIO

from ...services.logging import get_logger
from .protocol import read_message, write_message

log = get_logger(__name__)

Handler = Callable[[dict], dict]


def run_host(
    handler: Handler,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
) -> int:
    """Serve messages until stdin closes. Returns an exit code."""
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    while True:
        try:
            message = read_message(input_stream)
        except (ValueError, OSError) as exc:
            log.error("native_message_read_failed", error=str(exc))
            return 1
        if message is None:
            return 0
        try:
            response = handler(message)
        except Exception as exc:  # noqa: BLE001 - host must not crash
            log.exception("native_message_handler_failed")
            response = {"type": "error", "message": str(exc)}
        try:
            write_message(output_stream, response)
        except OSError as exc:
            log.error("native_message_write_failed", error=str(exc))
            return 1
