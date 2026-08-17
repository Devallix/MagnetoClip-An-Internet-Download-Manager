"""Tests for the native messaging protocol and host loop."""

from __future__ import annotations

import io
import threading
import time

import pytest

from magnetoclip.browser.native_messaging.host import run_host
from magnetoclip.browser.native_messaging.protocol import (
    decode_message,
    encode_message,
    read_message,
    write_message,
)


class _GatedInput(io.RawIOBase):
    """Input that withholds bytes until ``gate`` is set, so the outbound
    writer thread gets a chance to poll before the host drains stdin."""

    def __init__(self, data: bytes, gate: threading.Event) -> None:
        super().__init__()
        self._data = io.BytesIO(data)
        self._gate = gate

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        while not self._gate.is_set():
            time.sleep(0.01)
        return self._data.read(size)


def test_message_round_trip():
    stream = io.BytesIO()
    write_message(stream, {"type": "ping"})
    stream.seek(0)
    assert read_message(stream) == {"type": "ping"}


def test_encoded_decoded_round_trip():
    raw = encode_message({"a": 1, "b": [1, 2]})
    assert decode_message(raw) == {"a": 1, "b": [1, 2]}


def test_eof_returns_none():
    assert read_message(io.BytesIO()) is None


def test_truncated_payload_returns_none():
    import struct

    stream = io.BytesIO(struct.pack("<I", 100) + b"short")
    assert read_message(stream) is None


def test_oversized_message_rejected():
    import struct

    stream = io.BytesIO(struct.pack("<I", 1024 * 1024 * 20))
    with pytest.raises(ValueError):
        read_message(stream)


def test_unicode_survives():
    raw = encode_message({"filename": "caf\u00e9 \u65e5\u672c\u8a9e"})
    assert decode_message(raw)["filename"] == "caf\u00e9 \u65e5\u672c\u8a9e"


def test_run_host_serves_until_eof():
    requests: list[dict] = []
    inputs = io.BytesIO(
        encode_message({"type": "ping"})
        + encode_message({"type": "status"})
    )
    outputs = io.BytesIO()

    code = run_host(lambda msg: requests.append(msg) or {"ok": True}, inputs, outputs)

    assert code == 0
    assert requests == [{"type": "ping"}, {"type": "status"}]
    outputs.seek(0)
    assert read_message(outputs) == {"ok": True}
    assert read_message(outputs) == {"ok": True}
    assert read_message(outputs) is None


def test_run_host_handler_exception_returns_error():
    def bad_handler(msg):  # noqa: ANN001
        raise RuntimeError("boom")

    inputs = io.BytesIO(encode_message({"type": "ping"}))
    outputs = io.BytesIO()
    code = run_host(bad_handler, inputs, outputs)
    assert code == 0
    outputs.seek(0)
    response = read_message(outputs)
    assert response["type"] == "error"
    assert "boom" in response["message"]


def test_run_host_forwards_outbound_messages():
    queued = [
        {"type": "fetch_blob", "request_id": 1, "url": "blob:https://x/y"},
        {"type": "fetch_blob", "request_id": 2, "url": "blob:https://x/z"},
    ]
    gate = threading.Event()

    def outbound():
        item = queued.pop(0) if queued else None
        if item is not None and not queued:
            gate.set()  # writer drained the queue; now let the reader run
        return item

    inputs = _GatedInput(
        encode_message({"type": "ping"})
        + encode_message({"type": "ping"}),
        gate,
    )
    outputs = io.BytesIO()
    code = run_host(
        lambda msg: {"ok": True},
        inputs,
        outputs,
        outbound=outbound,
        outbound_interval=0.02,
    )
    assert code == 0

    outputs.seek(0)
    sent = []
    while True:
        message = read_message(outputs)
        if message is None:
            break
        sent.append(message)
    outbound_messages = [m for m in sent if m.get("type") == "fetch_blob"]
    assert outbound_messages == [
        {"type": "fetch_blob", "request_id": 1, "url": "blob:https://x/y"},
        {"type": "fetch_blob", "request_id": 2, "url": "blob:https://x/z"},
    ]
    assert len(sent) == 4  # 2 responses + 2 outbound


def test_run_host_survives_outbound_exceptions():
    calls = []

    def outbound():
        calls.append(1)
        raise RuntimeError("boom")

    inputs = io.BytesIO(encode_message({"type": "ping"}))
    outputs = io.BytesIO()
    code = run_host(
        lambda msg: {"ok": True},
        inputs,
        outputs,
        outbound=outbound,
        outbound_interval=0.02,
    )
    assert code == 0
    assert calls  # the writer thread polled at least once without crashing
