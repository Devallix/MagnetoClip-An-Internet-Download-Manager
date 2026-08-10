"""Schedule evaluation: time windows, day masks, and speed overrides."""

from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy.orm import sessionmaker

from ...app.context import AppContext
from ...database.models import Schedule
from ...database.repositories import ScheduleRepository
from ...services.logging import get_logger
from ..events.bus import Events

log = get_logger(__name__)

NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6


class Scheduler:
    """Decides whether scheduled windows are active and what speed to apply.

    A schedule is active when today's weekday is covered by ``days_mask`` and
    the current clock time falls inside ``[start_time, end_time)``. Windows
    that wrap past midnight (end <= start) are supported.
    """

    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.session_factory: sessionmaker = context.session_factory
        self._tick_task: asyncio.Task | None = None
        self._last_effective_bandwidth: float | None = None
        self._night_start = NIGHT_START_HOUR
        self._night_end = NIGHT_END_HOUR

    # ----- time helpers -----

    @staticmethod
    def _weekday_bit(now: datetime) -> int:
        return 1 << now.weekday()  # Monday=0 .. Sunday=6

    @staticmethod
    def _minutes(value: str | None) -> int | None:
        if not value:
            return None
        try:
            hour, minute = value.split(":")
            return int(hour) * 60 + int(minute)
        except (ValueError, TypeError):
            return None

    @classmethod
    def is_active(cls, schedule: Schedule, now: datetime | None = None) -> bool:
        if not schedule.enabled:
            return False
        now = now or datetime.now()
        if not (schedule.days_mask & cls._weekday_bit(now)):
            return False
        start = cls._minutes(schedule.start_time)
        end = cls._minutes(schedule.end_time)
        if start is None:
            return True  # no window means all day
        if end is None:
            end = start
        current = now.hour * 60 + now.minute
        if end > start:
            return start <= current < end
        return current >= start or current < end  # wraps midnight

    def bandwidth_for(self, schedule: Schedule, now: datetime | None = None) -> float | None:
        """Return the applicable speed in bytes/s for ``schedule`` or None."""
        if not schedule.enabled:
            return None
        now = now or datetime.now()
        speed_night = schedule.speed_night
        speed_day = schedule.speed_day
        if speed_night is None:
            return speed_day * 1024 * 1024 if speed_day else None
        is_night = now.hour >= self._night_start or now.hour < self._night_end
        chosen = speed_night if is_night else speed_day
        return chosen * 1024 * 1024 if chosen else None

    def effective_bandwidth(self, now: datetime | None = None) -> float | None:
        """Speed (bytes/s) for the first matching enabled schedule, else None."""
        for schedule in self.schedules():
            if self.is_active(schedule, now):
                speed = self.bandwidth_for(schedule, now)
                if speed is not None:
                    return speed
        return None

    def active_schedules(self, now: datetime | None = None) -> list[Schedule]:
        return [s for s in self.schedules() if self.is_active(s, now)]

    def schedules(self) -> list[Schedule]:
        with self.session_factory() as session:
            return ScheduleRepository(session).list()

    # ----- runtime -----

    async def start(self) -> None:
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

    async def apply_now(self) -> None:
        """Evaluate schedules and publish bandwidth changes."""
        bandwidth = self.effective_bandwidth()
        if bandwidth != self._last_effective_bandwidth:
            self._last_effective_bandwidth = bandwidth
            log.info("schedule_bandwidth_changed", bytes_per_second=bandwidth)
            self.context.events.post(
                Events.NETWORK_CHANGED, {"bandwidth_bytes_per_second": bandwidth}
            )

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self.apply_now()
            except Exception:  # noqa: BLE001 - never kill the scheduler
                log.exception("schedule_tick_failed")
            await asyncio.sleep(30)

    def request_toggle(self, enabled: bool) -> None:
        """Start or stop the runtime loop from a synchronous (UI) context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (e.g. tests) - nothing to do
        if enabled:
            loop.create_task(self.start())
            loop.create_task(self.apply_now())
        else:
            loop.create_task(self.stop())
