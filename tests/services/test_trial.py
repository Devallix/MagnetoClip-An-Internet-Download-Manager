"""Unit tests for the 7-day trial period logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from magnetoclip.services.licensing.trial import (
    ensure_trial_started,
    is_trial_active,
    trial_days_remaining,
    trial_end_date,
)


@pytest.fixture
def settings():
    """Minimal settings stub backed by a plain dict."""
    store = {
        "trial.first_launch": "",
        "trial.days": 7,
    }

    def _get(key, default=None):
        return store.get(key, default)

    def _set(key, value):
        store[key] = value

    stub = MagicMock()
    stub.get = _get
    stub.set = _set
    return stub


# ── ensure_trial_started ─────────────────────────────────────────────────


def test_ensure_trial_started_records_timestamp(settings):
    ensure_trial_started(settings)
    raw = settings.get("trial.first_launch")
    assert raw
    dt = datetime.fromisoformat(raw)
    assert dt.tzinfo is not None  # UTC-aware


def test_ensure_trial_started_idempotent(settings):
    ensure_trial_started(settings)
    first = settings.get("trial.first_launch")
    ensure_trial_started(settings)
    assert settings.get("trial.first_launch") == first


# ── is_trial_active / trial_days_remaining ────────────────────────────────


def test_trial_active_when_recently_started(settings):
    settings.set("trial.first_launch", datetime.now(UTC).isoformat())
    assert is_trial_active(settings) is True
    assert trial_days_remaining(settings) == 7


def test_trial_active_day_6(settings):
    launched = datetime.now(UTC) - timedelta(days=6)
    settings.set("trial.first_launch", launched.isoformat())
    assert is_trial_active(settings) is True
    assert trial_days_remaining(settings) == 1


def test_trial_expired_on_day_7(settings):
    launched = datetime.now(UTC) - timedelta(days=7)
    settings.set("trial.first_launch", launched.isoformat())
    assert is_trial_active(settings) is False
    assert trial_days_remaining(settings) == 0


def test_trial_expired_day_8(settings):
    launched = datetime.now(UTC) - timedelta(days=8)
    settings.set("trial.first_launch", launched.isoformat())
    assert is_trial_active(settings) is False
    assert trial_days_remaining(settings) == 0


def test_no_trial_if_first_launch_unset(settings):
    assert is_trial_active(settings) is False
    assert trial_days_remaining(settings) == 0


def test_custom_trial_days(settings):
    settings.set("trial.days", 3)
    settings.set("trial.first_launch", datetime.now(UTC).isoformat())
    assert is_trial_active(settings) is True
    assert trial_days_remaining(settings) == 3

    launched = datetime.now(UTC) - timedelta(days=3)
    settings.set("trial.first_launch", launched.isoformat())
    assert is_trial_active(settings) is False
    assert trial_days_remaining(settings) == 0


# ── trial_end_date ───────────────────────────────────────────────────────


def test_trial_end_date(settings):
    launched = datetime(2026, 8, 1, tzinfo=UTC)
    settings.set("trial.first_launch", launched.isoformat())
    end = trial_end_date(settings)
    assert end is not None
    assert end.isoformat() == "2026-08-08"


def test_trial_end_date_none_when_unset(settings):
    assert trial_end_date(settings) is None


def test_trial_end_date_custom_days(settings):
    launched = datetime(2026, 1, 1, tzinfo=UTC)
    settings.set("trial.first_launch", launched.isoformat())
    settings.set("trial.days", 14)
    end = trial_end_date(settings)
    assert end is not None
    assert end.isoformat() == "2026-01-15"
