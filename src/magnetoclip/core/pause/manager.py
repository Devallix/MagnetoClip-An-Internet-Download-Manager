"""PauseController: a simple pause switch with an optional auto-resume timer.

The legacy weekly time-window scheduler is gone. This replaces it with an
unambiguous manual control: pause all downloads now, optionally resume
automatically after N hours. State is persisted in settings so it survives
restarts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ...core.events.bus import Events
from ...database.repositories import SettingsStore
from ...services.logging import get_logger

log = get_logger(__name__)

_TICK_S = 15


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PauseController:
    """Own the pause switch and drive the one-shot auto-resume timer."""

    def __init__(self, context) -> None:
        self.context = context
        self.settings = context.settings
        self.events = context.events
        self._task: asyncio.Task | None = None
        self._start_loop()

    # ----- state -----

    def is_paused(self) -> bool:
        return bool(self.settings.get("pause.enabled", False))

    def auto_resume_hours(self) -> int:
        return max(0, int(self.settings.get("pause.auto_resume_hours", 0) or 0))

    def resume_at(self) -> datetime | None:
        raw = self.settings.get("pause.resume_at", "") or ""
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    # ----- actions -----

    def _persist(self) -> None:
        try:
            SettingsStore(self.context.session_factory).save_many(
                self.settings.to_store_dict()
            )
        except Exception:
            log.warning("pause_persist_failed", exc_info=True)

    def pause(self) -> None:
        hours = self.auto_resume_hours()
        resume_at = _utcnow() + timedelta(hours=hours) if hours > 0 else None
        self.settings.set("pause.resume_at", resume_at.isoformat() if resume_at else "")
        self.settings.set("pause.enabled", True)
        self._persist()
        self.events.post(
            Events.PAUSE_STATE_CHANGED,
            {
                "paused": True,
                "resume_at": resume_at.isoformat() if resume_at else None,
            },
        )
        log.info("pause_all_enabled", resume_at=str(resume_at))

    def resume(self) -> None:
        self.settings.set("pause.enabled", False)
        self.settings.set("pause.resume_at", "")
        self._persist()
        self.events.post(
            Events.PAUSE_STATE_CHANGED,
            {"paused": False, "resume_at": None},
        )
        log.info("pause_all_disabled")

    def set_auto_resume_hours(self, hours: int) -> None:
        hours = max(0, int(hours))
        self.settings.set("pause.auto_resume_hours", hours)
        resume_at = None
        if self.is_paused() and hours > 0:
            resume_at = _utcnow() + timedelta(hours=hours)
            self.settings.set("pause.resume_at", resume_at.isoformat())
        elif not self.is_paused():
            self.settings.set("pause.resume_at", "")
        self._persist()
        self.events.post(
            Events.PAUSE_STATE_CHANGED,
            {
                "paused": self.is_paused(),
                "resume_at": resume_at.isoformat() if resume_at else None,
            },
        )

    # ----- auto-resume loop -----

    def _start_loop(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop yet (e.g. headless construction). The GUI
            # startup path calls start_loop() once the qasync loop is live.
            self._task = None
            return
        self._task = asyncio.create_task(self._tick_loop())

    def start_loop(self) -> None:
        """Start the background tick if it isn't already running."""
        if self._task is not None and not self._task.done():
            return
        self._start_loop()

    async def _tick_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_TICK_S)
                self._check_resume()
            except asyncio.CancelledError:
                break
            except Exception:
                log.warning("pause_tick_failed", exc_info=True)

    def _check_resume(self) -> None:
        if not self.is_paused():
            return
        resume_at = self.resume_at()
        if resume_at is not None and _utcnow() >= resume_at:
            log.info("pause_auto_resume_fired")
            self.resume()

    def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None


__all__ = ["PauseController"]