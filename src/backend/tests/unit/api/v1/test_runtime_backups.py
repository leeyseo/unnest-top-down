import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langflow.api.v1 import runtime as runtime_module
from langflow.api.v1.runtime import (
    RuntimeBackupVerifyRequest,
    create_backup,
    list_backups,
    verify_backup,
)
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.runtime_configuration import RuntimeConfiguration
from langflow.services.database.models.user.model import User
from langflow.services.runtime_setup import generate_age_recovery_key


async def _configured_runtime(async_session, recipient: str) -> User:
    admin = User(
        username="runtime-admin",
        password="unused",  # noqa: S106
        is_superuser=True,
        is_active=True,
    )
    agent = Flow(name="agent", user_id=admin.id, data={"nodes": [], "edges": []})
    ingestion = Flow(name="ingestion", user_id=admin.id, data={"nodes": [], "edges": []})
    agent_version = FlowVersion(
        flow_id=agent.id,
        user_id=admin.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    ingestion_version = FlowVersion(
        flow_id=ingestion.id,
        user_id=admin.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    release = DeploymentRelease(
        user_id=admin.id,
        version="1.0.0",
        agent_flow_version_id=agent_version.id,
        ingestion_flow_version_id=ingestion_version.id,
        config={},
        manifest={},
        api_version="v1",
    )
    configuration = RuntimeConfiguration(
        id=1,
        setup_complete=True,
        settings={"backup_recipient": recipient},
        encrypted_secrets="encrypted",
        master_key_fingerprint="0" * 64,
        created_by_user_id=admin.id,
    )
    async_session.add_all(
        [
            admin,
            agent,
            ingestion,
            agent_version,
            ingestion_version,
            release,
            configuration,
        ]
    )
    await async_session.flush()
    return admin


async def test_admin_creates_lists_and_verifies_encrypted_backup(
    async_session,
    monkeypatch,
    tmp_path,
):
    identity, recipient = generate_age_recovery_key()
    admin = await _configured_runtime(async_session, recipient)
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('ready')")
    master_key = tmp_path / "master.key"
    master_key.write_bytes(b"runtime-master-key")
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("UNNEST_MASTER_KEY_FILE", str(master_key))
    monkeypatch.setattr(
        runtime_module,
        "get_db_service",
        lambda: SimpleNamespace(database_url=f"sqlite:///{database}"),
    )

    backup = await create_backup(session=async_session, admin=admin)
    listed = await list_backups(admin)
    verified = await verify_backup(
        backup_id=backup.id,
        payload=RuntimeBackupVerifyRequest(recovery_identity=identity),
        session=async_session,
        admin=admin,
    )

    assert [item.id for item in listed] == [backup.id]
    assert verified == {
        "valid": True,
        "backup_id": backup.id,
        "release_version": "1.0.0",
        "created_at": verified["created_at"],
        "database_format": "sqlite3",
    }

    wrong_identity, _wrong_recipient = generate_age_recovery_key()
    with pytest.raises(HTTPException) as exc:
        await verify_backup(
            backup_id=backup.id,
            payload=RuntimeBackupVerifyRequest(recovery_identity=wrong_identity),
            session=async_session,
            admin=admin,
        )
    assert exc.value.status_code == 422
