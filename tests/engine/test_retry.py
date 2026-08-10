import httpx

from magnetoclip.engine.retry.policy import (
    PermanentError,
    RetryableError,
    SegmentAborted,
    backoff_delay,
    classify_http_status,
    is_retryable_exception,
    is_retryable_http_status,
)


def test_backoff_increases_with_attempts():
    delays = [backoff_delay(i, jitter=0) for i in range(1, 5)]
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 4.0
    assert delays[3] == 8.0


def test_backoff_capped():
    assert backoff_delay(10, cap=60, jitter=0) == 60.0


def test_backoff_has_jitter():
    samples = {backoff_delay(2) for _ in range(20)}
    assert len(samples) > 1


def test_retryable_statuses():
    assert is_retryable_http_status(503)
    assert is_retryable_http_status(429)
    assert not is_retryable_http_status(404)
    assert not is_retryable_http_status(200)


def test_classify_status():
    assert classify_http_status(503) is RetryableError
    assert classify_http_status(404) is PermanentError


def test_is_retryable_exception():
    assert is_retryable_exception(httpx.ConnectError("x"))
    assert is_retryable_exception(RetryableError())
    assert not is_retryable_exception(PermanentError())
    assert not is_retryable_exception(SegmentAborted())
