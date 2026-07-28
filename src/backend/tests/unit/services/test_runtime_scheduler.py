from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.message.model import MessageTable
from langflow.services.database.models.runtime_schedule import RuntimeSchedule
from langflow.services.database.models.user.model import User
from langflow.services.runtime_conversation import purge_expired_runtime_conversations
from langflow.services.runtime_scheduler import RuntimeScheduler, next_cron_run
from sqlmodel import select


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

    async def maintain(_now):
        return 0

    await RuntimeScheduler(executor=execute, maintenance=maintain).run_due(now)
    async_session.expire_all()
    refreshed_due = await async_session.get(RuntimeSchedule, due_id)
    refreshed_disabled = await async_session.get(RuntimeSchedule, disabled_id)

    assert executed == [due_id]
    assert refreshed_due is not None
    assert refreshed_due.last_status == "success"
    assert refreshed_due.next_run_at > now.replace(tzinfo=None)
    assert refreshed_disabled is not None
    assert refreshed_disabled.last_status is None


async def test_runtime_conversation_retention_deletes_only_expired_release_messages(async_session):
    now = datetime.now(timezone.utc)
    user = User(username="retention-owner", password="unused", is_active=True)  # noqa: S106
    flow = Flow(name="retention-agent", user_id=user.id, data={"nodes": [], "edges": []})
    version = FlowVersion(
        flow_id=flow.id,
        user_id=user.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    release = DeploymentRelease(
        user_id=user.id,
        version="1.0.0",
        agent_flow_version_id=version.id,
        ingestion_flow_version_id=version.id,
        api_version="v1",
        config={"store_conversations": True, "conversation_retention_days": 30},
        manifest={
            "deployment": {
                "store_conversations": True,
                "conversation_retention_days": 30,
            }
        },
    )
    expired = MessageTable(
        sender="User",
        sender_name="User",
        text="expired",
        session_id="expired",
        flow_id=flow.id,
        timestamp=now - timedelta(days=31),
        session_metadata={"api_version": "v1"},
    )
    current = MessageTable(
        sender="User",
        sender_name="User",
        text="current",
        session_id="current",
        flow_id=flow.id,
        timestamp=now - timedelta(days=29),
        session_metadata={"api_version": "v1"},
    )
    unrelated = MessageTable(
        sender="User",
        sender_name="User",
        text="other api",
        session_id="other",
        flow_id=flow.id,
        timestamp=now - timedelta(days=31),
        session_metadata={"api_version": "v2"},
    )
    async_session.add_all([user, flow, version, release, expired, current, unrelated])
    await async_session.commit()

    deleted = await purge_expired_runtime_conversations(async_session, now=now)
    await async_session.commit()
    remaining = (await async_session.exec(select(MessageTable.session_id))).all()

    assert deleted == 1
    assert set(remaining) == {"current", "other"}


async def test_runtime_scheduler_runs_maintenance_at_most_hourly(async_session, monkeypatch):
    now = datetime.now(timezone.utc)
    maintained = []

    async def maintain(at):
        maintained.append(at)
        return 0

    @asynccontextmanager
    async def test_session_scope():
        yield async_session

    monkeypatch.setattr("langflow.services.runtime_scheduler.session_scope", test_session_scope)
    scheduler = RuntimeScheduler(maintenance=maintain)

    await scheduler.run_due(now)
    await scheduler.run_due(now + timedelta(minutes=59))
    await scheduler.run_due(now + timedelta(hours=1))

    assert maintained == [now, now + timedelta(hours=1)]
