"""Persistent "skip all" suppression for the capture confirmation dialog.

Clicking "Skip all" in the capture dialog must silence future confirmation
popups, not just the captures already waiting in the database. This module owns
that state, stored as a UTC timestamp in the ``browser.skip_all_until``
setting: while it is in the future, new browser captures are auto-rejected
instead of being enqueued for confirmation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

SETTING_KEY = "browser.skip_all_until"

SKIP_ALL_DURATION = timedelta(hours=1)

_PERSISTENT_UNTIL = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def skip_all_until(context) -> datetime | None:
    """Return the configured skip-all deadline, or None if disabled."""
    raw = context.settings.get(SETTING_KEY, "")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def skip_all_active(context) -> bool:
    """True while future captures should be auto-rejected."""
    until = skip_all_until(context)
    if until is None:
        return False
    return datetime.now(timezone.utc) < until


def enable_skip_all(context, *, duration: timedelta | None = SKIP_ALL_DURATION) -> None:
    """Suppress capture dialogs; ``duration=None`` disables them until turned off."""
    if duration is None:
        until = _PERSISTENT_UNTIL
    else:
        until = datetime.now(timezone.utc) + duration
    _store(context, until)


def disable_skip_all(context) -> None:
    """Re-enable capture dialogs immediately."""
    _store(context, None)


def _store(context, until: datetime | None) -> None:
    from magnetoclip.database.repositories import SettingsStore

    value = until.isoformat() if until is not None else ""
    context.settings.set(SETTING_KEY, value)
    SettingsStore(context.session_factory).save(SETTING_KEY, value)
