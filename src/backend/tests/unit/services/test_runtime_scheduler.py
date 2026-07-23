from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from langflow.services.database.models.runtime_schedule import RuntimeSchedule
from langflow.services.runtime_scheduler import RuntimeScheduler, next_cron_run


def test_next_cron_run_validates_expression_and_timezone():
    after = datetime(2026, 7, 23, tzinfo=timezone.utc)

    assert next_cron_run("*/5 * * * *", "Asia/Seoul", after) == datetime(
        2026,
        7,
        23,
        0,
        5,
        tzinfo=timezone.utc,
    )
    with pytest.raises(ValueError, match="Invalid cron"):
        next_cron_run("not-a-cron", "UTC", after)
    with pytest.raises(ValueError, match="Invalid cron"):
        next_cron_run("* * * * *", "Missing/Timezone", after)


async def test_runtime_scheduler_executes_only_due_enabled_schedules(async_session, monkeypatch):
    now = datetime.now(timezone.utc)
    due = RuntimeSchedule(
        name="due",
        cron_expression="*/5 * * * *",
        timezone="UTC",
        api_version="v1",
        request_payload={"message": "scheduled"},
        enabled=True,
        next_run_at=now - timedelta(minutes=1),
    )
    disabled = RuntimeSchedule(
        name="disabled",
        cron_expression="*/5 * * * *",
        timezone="UTC",
        api_version="v1",
        request_payload={"message": "disabled"},
        enabled=False,
        next_run_at=now - timedelta(minutes=1),
    )
    async_session.add_all([due, disabled])
    await async_session.commit()
    due_id = due.id
    disabled_id = disabled.id
    executed = []

    async def execute(schedule_id):
        executed.append(schedule_id)

    @asynccontextmanager
    async def test_session_scope():
        yield async_session
        await async_session.commit()

    monkeypatch.setattr("langflow.services.runtime_scheduler.session_scope", test_session_scope)
    await RuntimeScheduler(executor=execute).run_due(now)
    async_session.expire_all()
    refreshed_due = await async_session.get(RuntimeSchedule, due_id)
    refreshed_disabled = await async_session.get(RuntimeSchedule, disabled_id)

    assert executed == [due_id]
    assert refreshed_due is not None
    assert refreshed_due.last_status == "success"
    assert refreshed_due.next_run_at > now.replace(tzinfo=None)
    assert refreshed_disabled is not None
    assert refreshed_disabled.last_status is None
