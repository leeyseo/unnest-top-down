import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi import BackgroundTasks, HTTPException
from langflow.api.v1 import runtime as runtime_module
from langflow.api.v1.runtime import RuntimeSetupRequest, _setup_complete, complete_runtime_setup
from langflow.services.database.models import Job
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.runtime_configuration import RuntimeConfiguration
from langflow.services.database.models.runtime_document import DocumentVersion, RuntimeDocument
from langflow.services.database.models.user.model import User
from langflow.services.runtime_setup import decrypt_runtime_secrets
from sqlmodel import select


async def _release(
    async_session,
    *,
    secret_names: list[str],
    source_id: UUID | None = None,
) -> DeploymentRelease:
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
        config={
            "tls": "self-signed",
            "default_language": "en",
            "allow_language_switch": False,
            "branding": {"solution_name": "Agency Agent", "show_unnest_branding": False},
        },
        manifest={
            "secret_names": secret_names,
            "source_documents": [{"id": str(source_id)}] if source_id else [],
        },
        api_version="v1",
    )
    async_session.add_all([owner, agent, ingestion, agent_version, ingestion_version, release])
    await async_session.flush()
    if source_id:
        knowledge_base = KnowledgeBaseRecord(name="shared", user_id=owner.id)
        document_id = uuid5(release.id, str(source_id))
        async_session.add(knowledge_base)
        await async_session.flush()
        async_session.add_all(
            [
                RuntimeDocument(
                    id=document_id,
                    user_id=owner.id,
                    knowledge_base_id=knowledge_base.id,
                    name="guide.txt",
                    status="pending",
                ),
                DocumentVersion(
                    id=uuid5(document_id, "v1"),
                    document_id=document_id,
                    version_number=1,
                    checksum=f"sha256:{'a' * 64}",
                    mime_type="text/plain",
                    size_bytes=5,
                    storage_path=f"runtime-documents/{document_id.hex}.source",
                    status="pending",
                ),
            ]
        )
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
        BackgroundTasks(),
        async_session,
    )

    configuration = await async_session.get(RuntimeConfiguration, 1)
    admin = (
        await async_session.exec(select(User).where(User.username == "runtime-admin"))
    ).one()
    assert result["complete"] is True
    assert result["api_versions"] == ["v1"]
    assert result["default_language"] == "en"
    assert result["allow_language_switch"] is False
    assert result["branding"]["solution_name"] == "Agency Agent"
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
            BackgroundTasks(),
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
            BackgroundTasks(),
            async_session,
        )

    assert exc.value.status_code == 422
    assert "MODEL_TOKEN" in str(exc.value.detail)
    assert not key_path.exists()


async def test_runtime_setup_rejects_missing_bundled_document_before_writing_key(
    async_session,
    monkeypatch,
    tmp_path,
):
    source_id = uuid4()
    release = await _release(async_session, secret_names=[], source_id=source_id)
    document_id = uuid5(release.id, str(source_id))
    version = await async_session.get(DocumentVersion, uuid5(document_id, "v1"))
    document = await async_session.get(RuntimeDocument, document_id)
    assert version is not None
    assert document is not None
    await async_session.delete(version)
    await async_session.delete(document)
    await async_session.flush()
    _patch_setup_services(monkeypatch)
    key_path = tmp_path / "master.key"
    monkeypatch.setenv("UNNEST_MASTER_KEY_FILE", str(key_path))

    with pytest.raises(HTTPException) as exc:
        await complete_runtime_setup(
            RuntimeSetupRequest(
                admin_username="runtime-admin",
                admin_password="strong-password",  # noqa: S106
            ),
            BackgroundTasks(),
            async_session,
        )

    assert exc.value.status_code == 503
    assert str(source_id) in str(exc.value.detail)
    assert not key_path.exists()


async def test_runtime_setup_queues_bundled_document_ingestion(
    async_session,
    monkeypatch,
    tmp_path,
):
    source_id = uuid4()
    release = await _release(async_session, secret_names=[], source_id=source_id)
    _patch_setup_services(monkeypatch)
    monkeypatch.setenv("UNNEST_MASTER_KEY_FILE", str(tmp_path / "master.key"))
    schedule_ingestion = AsyncMock()
    monkeypatch.setattr(runtime_module, "_schedule_runtime_ingestion", schedule_ingestion)
    background = BackgroundTasks()

    result = await complete_runtime_setup(
        RuntimeSetupRequest(
            admin_username="runtime-admin",
            admin_password="strong-password",  # noqa: S106
        ),
        background,
        async_session,
    )
    jobs = (await async_session.exec(select(Job))).all()
    ingestion_version = await async_session.get(FlowVersion, release.ingestion_flow_version_id)

    assert result["complete"] is True
    assert result["bundled_documents_ready"] is False
    assert result["bundled_ingestion_job_ids"] == [str(jobs[0].job_id)]
    assert ingestion_version is not None
    assert jobs[0].flow_id == ingestion_version.flow_id
    assert jobs[0].job_metadata["bundled"] is True

    await background()

    schedule_ingestion.assert_awaited_once_with(
        release_id=release.id,
        document_id=uuid5(release.id, str(source_id)),
        version_id=uuid5(uuid5(release.id, str(source_id)), "v1"),
        user_id=release.user_id,
        job_id=jobs[0].job_id,
    )
