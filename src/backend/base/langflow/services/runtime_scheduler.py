"""Single-leader cron scheduler for an on-premise runtime."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lfx.log.logger import logger
from sqlalchemy import text
from sqlmodel import col, select

from langflow.services.database.models.runtime_schedule import RuntimeSchedule
from langflow.services.deps import session_scope
from langflow.services.runtime_conversation import purge_expired_runtime_conversations

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

_SCHEDULER_LOCK_ID = 0x554E4E455353
_SUNDAY_ALIAS = 7
_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_WEEKDAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MAX_CRON_SEARCH_MINUTES = 5 * 366 * 24 * 60
_MAINTENANCE_INTERVAL = timedelta(hours=1)


def _cron_number(value: str, aliases: dict[str, int] | None) -> int:
    normalized = value.lower()
    if aliases and normalized in aliases:
        return aliases[normalized]
    return int(normalized)


def _cron_field(
    expression: str,
    minimum: int,
    maximum: int,
    *,
    aliases: dict[str, int] | None = None,
    sunday_is_seven: bool = False,
) -> set[int]:
    values: set[int] = set()
    for raw_part in expression.split(","):
        part, separator, raw_step = raw_part.partition("/")
        step = int(raw_step) if separator else 1
        if step <= 0:
            raise ValueError
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            raw_start, raw_end = part.split("-", 1)
            start = _cron_number(raw_start, aliases)
            end = _cron_number(raw_end, aliases)
        else:
            start = _cron_number(part, aliases)
            end = maximum if separator else start
        allowed_maximum = _SUNDAY_ALIAS if sunday_is_seven else maximum
        if start < minimum or end > allowed_maximum or start > end:
            raise ValueError
        values.update(
            0 if sunday_is_seven and value == _SUNDAY_ALIAS else value
            for value in range(start, end + 1, step)
        )
    if not values:
        raise ValueError
    return values


def next_cron_run(expression: str, timezone_name: str, after: datetime | None = None) -> datetime:
    try:
        timezone_info = ZoneInfo(timezone_name)
        minute_raw, hour_raw, day_raw, month_raw, weekday_raw = expression.split()
        minutes = _cron_field(minute_raw, 0, 59)
        hours = _cron_field(hour_raw, 0, 23)
        days = _cron_field(day_raw, 1, 31)
        months = _cron_field(month_raw, 1, 12, aliases=_MONTH_NAMES)
        weekdays = _cron_field(
            weekday_raw,
            0,
            6,
            aliases=_WEEKDAY_NAMES,
            sunday_is_seven=True,
        )
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        msg = "Invalid cron expression or timezone"
        raise ValueError(msg) from exc
    now = after or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    candidate = (now.astimezone(timezone.utc) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    day_is_wildcard = day_raw == "*"
    weekday_is_wildcard = weekday_raw == "*"
    for _ in range(_MAX_CRON_SEARCH_MINUTES):
        local = candidate.astimezone(timezone_info)
        day_match = local.day in days
        weekday_match = ((local.weekday() + 1) % 7) in weekdays
        if day_is_wildcard:
            date_matches = weekday_match
        elif weekday_is_wildcard:
            date_matches = day_match
        else:
            date_matches = day_match or weekday_match
        if local.minute in minutes and local.hour in hours and local.month in months and date_matches:
            return candidate
        candidate += timedelta(minutes=1)
    msg = "Cron expression has no run within five years"
    raise ValueError(msg)


async def _execute_schedule(schedule_id: UUID) -> None:
    from langflow.api.v1.runtime import execute_scheduled_agent

    await execute_scheduled_agent(schedule_id)


async def _purge_conversations(now: datetime) -> int:
    async with session_scope() as session:
        return await purge_expired_runtime_conversations(session, now=now)


class RuntimeScheduler:
    def __init__(
        self,
        *,
        executor: Callable[[UUID], Awaitable[None]] = _execute_schedule,
        maintenance: Callable[[datetime], Awaitable[int]] = _purge_conversations,
        poll_seconds: float = 15.0,
    ) -> None:
        self.executor = executor
        self.maintenance = maintenance
        self.poll_seconds = poll_seconds
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_maintenance_at: datetime | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop is not None:
            self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=30)
        except TimeoutError:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _wait(self) -> None:
        if self._stop is None:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)

    async def _run(self) -> None:
        while self._stop is not None and not self._stop.is_set():
            try:
                await self._leadership_cycle()
            except Exception:  # noqa: BLE001 - scheduler retries after an operator-visible error
                await logger.aexception("Runtime scheduler leadership cycle failed")
            await self._wait()

    async def _leadership_cycle(self) -> None:
        async with session_scope() as leadership_session:
            postgres = leadership_session.get_bind().dialect.name == "postgresql"
            if postgres:
                acquired = (
                    await leadership_session.execute(
                        text("SELECT pg_try_advisory_lock(:lock_id)"),
                        {"lock_id": _SCHEDULER_LOCK_ID},
                    )
                ).scalar()
                if not acquired:
                    return
            try:
                while self._stop is not None and not self._stop.is_set():
                    await self.run_due()
                    await self._wait()
            finally:
                if postgres:
                    await leadership_session.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": _SCHEDULER_LOCK_ID},
                    )

    async def run_due(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        async with session_scope() as session:
            due = (
                await session.exec(
                    select(RuntimeSchedule).where(
                        RuntimeSchedule.enabled.is_(True),
                        col(RuntimeSchedule.next_run_at) <= current,
                    )
                )
            ).all()
            schedule_ids = []
            for schedule in due:
                schedule.next_run_at = next_cron_run(schedule.cron_expression, schedule.timezone, current)
                schedule.last_started_at = current
                schedule.last_status = "running"
                schedule.last_error = None
                schedule.updated_at = current
                session.add(schedule)
                schedule_ids.append(schedule.id)

        for schedule_id in schedule_ids:
            status = "success"
            error = None
            try:
                await self.executor(schedule_id)
            except Exception as exc:  # noqa: BLE001 - failure is persisted and the next run remains scheduled
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"[:500]
                await logger.aexception("Runtime schedule %s failed", schedule_id)
            async with session_scope() as session:
                schedule = await session.get(RuntimeSchedule, schedule_id)
                if schedule is not None:
                    schedule.last_finished_at = datetime.now(timezone.utc)
                    schedule.last_status = status
                    schedule.last_error = error
                    schedule.updated_at = datetime.now(timezone.utc)
                    session.add(schedule)
        if self._last_maintenance_at is None or current - self._last_maintenance_at >= _MAINTENANCE_INTERVAL:
            await self.maintenance(current)
            self._last_maintenance_at = current


_runtime_scheduler = RuntimeScheduler()


def get_runtime_scheduler() -> RuntimeScheduler:
    return _runtime_scheduler
