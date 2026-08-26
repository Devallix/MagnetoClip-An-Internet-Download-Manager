"""7-day trial period logic.

The first time the app is launched, ``ensure_trial_started()`` records the
current UTC timestamp.  For the next *N* days (default 7, configurable via
``trial.days``), the activation gate is bypassed and the user has full
access.  Once the trial expires the normal licensing gate kicks in.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from magnetoclip.services.logging.setup import get_logger

log = get_logger(__name__)


def ensure_trial_started(settings) -> None:
    """Record the first-launch timestamp if not already set.

    This is idempotent — calling it multiple times has no effect after the
    first call.
    """
    if settings.get("trial.first_launch"):
        return
    settings.set("trial.first_launch", datetime.now(UTC).isoformat())
    log.info("trial_started", first_launch=settings.get("trial.first_launch"))


def _first_launch_dt(settings) -> datetime | None:
    raw = str(settings.get("trial.first_launch") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def is_trial_active(settings) -> bool:
    """Return *True* if the trial window has not yet expired."""
    remaining = trial_days_remaining(settings)
    return remaining > 0


def trial_days_remaining(settings) -> int:
    """Return the number of whole trial days left (≥ 0).

    Uses ceiling so any partial day still counts as a full day.
    """
    first = _first_launch_dt(settings)
    if first is None:
        return 0
    days = int(settings.get("trial.days") or 7)
    deadline = first + timedelta(days=days)
    now = datetime.now(UTC)
    remaining = (deadline - now).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining / 86400))


def trial_end_date(settings) -> date | None:
    """Return the calendar date the trial expires, or *None* if unset."""
    first = _first_launch_dt(settings)
    if first is None:
        return None
    days = int(settings.get("trial.days") or 7)
    return (first + timedelta(days=days)).date()
