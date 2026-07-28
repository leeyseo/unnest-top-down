import pytest
from fastapi import HTTPException
from langflow.api.v1.runtime import (
    RuntimeUserCreate,
    RuntimeUserUpdate,
    create_runtime_user,
    delete_runtime_user,
    update_runtime_user,
)
from langflow.services.database.models.runtime_audit import RuntimeAuditEvent
from langflow.services.database.models.user.model import User
from sqlmodel import select


async def test_runtime_admin_manages_local_users_without_exposing_password(async_session, monkeypatch):
    user_password = "general-password"  # noqa: S105

    class Auth:
        @staticmethod
        def get_password_hash(password):
            return f"hashed:{password}"

    monkeypatch.setattr("langflow.api.v1.runtime.get_auth_service", lambda: Auth())
    admin = User(
        username="runtime-admin",
        password="hashed:admin-password",  # noqa: S106
        is_superuser=True,
        is_active=True,
    )
    async_session.add(admin)
    await async_session.flush()

    created = await create_runtime_user(
        RuntimeUserCreate(
            username="general-user",
            password=user_password,
            role="general",
        ),
        async_session,
        admin,
    )
    stored = await async_session.get(User, created.id)

    assert created.role == "general"
    assert "password" not in created.model_dump()
    assert stored is not None
    assert stored.password != user_password
    assert stored.password == f"hashed:{user_password}"

    updated = await update_runtime_user(
        created.id,
        RuntimeUserUpdate(role="admin", is_active=False),
        async_session,
        admin,
    )
    assert updated.role == "admin"
    assert updated.is_active is False

    await delete_runtime_user(created.id, async_session, admin)
    assert await async_session.get(User, created.id) is None
    events = (await async_session.exec(select(RuntimeAuditEvent))).all()
    assert [event.event_type for event in events] == ["user.created", "user.updated", "user.deleted"]


async def test_runtime_admin_cannot_remove_itself(async_session):
    admin = User(
        username="only-admin",
        password="hashed:admin-password",  # noqa: S106
        is_superuser=True,
        is_active=True,
    )
    async_session.add(admin)
    await async_session.flush()

    with pytest.raises(HTTPException, match="cannot delete itself"):
        await delete_runtime_user(admin.id, async_session, admin)
