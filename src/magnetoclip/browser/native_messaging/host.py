"""Native messaging host process: read requests, write responses.

The host normally only answers extension-initiated messages, but it can also
push messages to the extension (e.g. asking it to fetch a ``blob:`` URL the
user pasted into the app). Pass an ``outbound`` provider and a writer thread
polls it and forwards anything it returns to the browser.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import BinaryIO

from ...services.logging import get_logger
from .protocol import read_message, write_message

log = get_logger(__name__)

Handler = Callable[[dict], dict]
OutboundProvider = Callable[[], dict | None]


def run_host(
    handler: Handler,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    *,
    outbound: OutboundProvider | None = None,
    outbound_interval: float = 0.2,
) -> int:
    """Serve messages until stdin closes. Returns an exit code.

    ``outbound`` (optional) returns the next message to send to the extension,
    or ``None``. It is polled on a background thread so the app can push
    requests to the browser between extension-initiated exchanges.
    """
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    write_lock = threading.Lock()
    stop_event = threading.Event()

    writer: threading.Thread | None = None
    if outbound is not None:

        def _writer_loop() -> None:
            while not stop_event.is_set():
                try:
                    message = outbound()
                except Exception:  # noqa: BLE001 - a bad outbound message must not kill the host
                    log.exception("outbound_provider_failed")
                    message = None
                if message is not None:
                    try:
                        with write_lock:
                            write_message(output_stream, message)
                    except OSError:
                        return
                stop_event.wait(outbound_interval)

        writer = threading.Thread(target=_writer_loop, daemon=True)
        writer.start()

    try:
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
                with write_lock:
                    write_message(output_stream, response)
            except OSError as exc:
                log.error("native_message_write_failed", error=str(exc))
                return 1
    finally:
        if writer is not None:
            stop_event.set()
            writer.join(timeout=max(outbound_interval * 2, 1.0))
