import stat
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langflow.api.v1 import runtime as runtime_module
from langflow.api.v1.runtime import RuntimeSetupRequest, _setup_complete, complete_runtime_setup
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.runtime_configuration import RuntimeConfiguration
from langflow.services.database.models.user.model import User
from langflow.services.runtime_setup import decrypt_runtime_secrets
from sqlmodel import select


async def _release(async_session, *, secret_names: list[str]) -> DeploymentRelease:
    owner = User(username="release-owner", password="unused", is_active=False)  # noqa: S106
    agent = Flow(name="agent", user_id=owner.id, data={"nodes": [], "edges": []})
    ingestion = Flow(name="ingestion", user_id=owner.id, data={"nodes": [], "edges": []})
    agent_version = FlowVersion(
        flow_id=agent.id,
        user_id=owner.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    ingestion_version = FlowVersion(
        flow_id=ingestion.id,
        user_id=owner.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    release = DeploymentRelease(
        user_id=owner.id,
        version="1.0.0",
        agent_flow_version_id=agent_version.id,
        ingestion_flow_version_id=ingestion_version.id,
        config={"tls": "self-signed"},
        manifest={"secret_names": secret_names},
        api_version="v1",
    )
    async_session.add_all([owner, agent, ingestion, agent_version, ingestion_version, release])
    await async_session.flush()
    return release


def _patch_setup_services(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_module,
        "runtime_license_status",
        lambda _release=None: {"valid": True, "reason": None},
    )
    monkeypatch.setattr(
        runtime_module,
        "get_auth_service",
        lambda: SimpleNamespace(get_password_hash=lambda value: f"hashed:{value}"),
    )


async def test_runtime_setup_persists_encrypted_secrets_and_first_admin(
    async_session,
    monkeypatch,
    tmp_path,
):
    await _release(async_session, secret_names=["MODEL_TOKEN"])
    _patch_setup_services(monkeypatch)
    key_path = tmp_path / "secrets" / "master.key"
    monkeypatch.setenv("UNNEST_MASTER_KEY_FILE", str(key_path))
    monkeypatch.delenv("UNNEST_RUNTIME_SETUP_COMPLETE", raising=False)

    result = await complete_runtime_setup(
        RuntimeSetupRequest(
            admin_username="runtime-admin",
            admin_password="strong-password",  # noqa: S106
            secret_values={"MODEL_TOKEN": "top-secret"},
        ),
        async_session,
    )

    configuration = await async_session.get(RuntimeConfiguration, 1)
    admin = (
        await async_session.exec(select(User).where(User.username == "runtime-admin"))
    ).one()
    assert result["complete"] is True
    assert result["recovery_identity"].startswith("AGE-SECRET-KEY-1")
    assert configuration is not None
    assert "top-secret" not in configuration.encrypted_secrets
    assert decrypt_runtime_secrets(configuration) == {"MODEL_TOKEN": "top-secret"}
    assert configuration.settings["backup_recipient"].startswith("age1")
    assert "AGE-SECRET-KEY-" not in str(configuration.settings)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert admin.is_superuser is True
    assert admin.password == "hashed:strong-password"  # noqa: S105
    assert await _setup_complete(async_session) is True

    with pytest.raises(HTTPException) as exc:
        await complete_runtime_setup(
            RuntimeSetupRequest(
                admin_username="another-admin",
                admin_password="strong-password",  # noqa: S106
                secret_values={"MODEL_TOKEN": "different"},
            ),
            async_session,
        )
    assert exc.value.status_code == 409


async def test_runtime_setup_rejects_missing_declared_secret_before_writing_key(
    async_session,
    monkeypatch,
    tmp_path,
):
    await _release(async_session, secret_names=["MODEL_TOKEN"])
    _patch_setup_services(monkeypatch)
    key_path = tmp_path / "master.key"
    monkeypatch.setenv("UNNEST_MASTER_KEY_FILE", str(key_path))

    with pytest.raises(HTTPException) as exc:
        await complete_runtime_setup(
            RuntimeSetupRequest(
                admin_username="runtime-admin",
                admin_password="strong-password",  # noqa: S106
            ),
            async_session,
        )

    assert exc.value.status_code == 422
    assert "MODEL_TOKEN" in str(exc.value.detail)
    assert not key_path.exists()
