from __future__ import annotations

import random

import httpx


class RetryableError(Exception):
    """A transient error that may succeed on retry."""


class PermanentError(Exception):
    """A non-recoverable error."""


class SegmentAborted(Exception):
    """The segment was aborted by pause/cancel."""


class RangeNotSupported(Exception):
    """The server does not honor Range requests."""


RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_retryable_http_status(status: int) -> bool:
    return status in RETRYABLE_HTTP_STATUS


def is_retryable_exception(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (httpx.TransportError, httpx.TimeoutException, RetryableError),
    )


def classify_http_status(status: int) -> type[Exception]:
    """Return the exception class appropriate for an HTTP status code."""
    if is_retryable_http_status(status):
        return RetryableError
    return PermanentError


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    jitter: float = 0.25,
) -> float:
    """Exponential backoff with jitter: ``base * 2^(attempt-1)`` capped."""
    exponential = min(base * (2 ** max(0, attempt - 1)), cap)
    return exponential * (1 + random.uniform(0, jitter))
