from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.runtime_audit import RuntimeAuditEvent
from langflow.services.database.models.runtime_document import DocumentVersion, IndexGeneration, RuntimeDocument
from langflow.services.database.models.user.model import User
from langflow.services.runtime_document import (
    activate_document_version,
    activate_index_generation,
    create_shadow_generation,
    fail_document_version,
    fail_index_generation,
    move_document_to_trash,
    purge_expired_runtime_documents,
    register_document,
)
from sqlmodel import select


async def test_document_versions_and_shadow_index_switch_atomically(async_session):
    user = User(username="runtime-doc-owner", password="unused", is_active=True)  # noqa: S106
    kb = KnowledgeBaseRecord(name="shared", user_id=user.id)
    async_session.add_all([user, kb])
    await async_session.flush()

    document, first, created = await register_document(
        async_session,
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="policy.pdf",
        checksum=f"sha256:{'a' * 64}",
        mime_type="application/pdf",
        size_bytes=100,
        storage_path="runtime-documents/policy-v1.pdf",
    )
    assert created is True
    await activate_document_version(async_session, document_id=document.id, version_id=first.id)

    duplicate_document, duplicate, created = await register_document(
        async_session,
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="copy.pdf",
        checksum=first.checksum,
        mime_type=first.mime_type,
        size_bytes=first.size_bytes,
        storage_path="runtime-documents/copy.pdf",
        duplicate_strategy="skip",
    )
    assert created is False
    assert duplicate_document.id == document.id
    assert duplicate.id == first.id

    document, second, created = await register_document(
        async_session,
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="policy.pdf",
        checksum=f"sha256:{'b' * 64}",
        mime_type="application/pdf",
        size_bytes=120,
        storage_path="runtime-documents/policy-v2.pdf",
        duplicate_strategy="new_version",
    )
    assert created is True
    assert second.version_number == 2
    await activate_document_version(async_session, document_id=document.id, version_id=second.id)
    await async_session.refresh(first)
    assert first.status == "superseded"
    assert second.status == "active"

    old_generation, created = await create_shadow_generation(
        async_session,
        knowledge_base_id=kb.id,
        fingerprint="sha256:old",
    )
    assert created is True
    await activate_index_generation(
        async_session,
        generation=old_generation,
        backend_reference={"collection": "generation-old"},
    )
    new_generation, _created = await create_shadow_generation(
        async_session,
        knowledge_base_id=kb.id,
        fingerprint="sha256:new",
    )
    await activate_index_generation(
        async_session,
        generation=new_generation,
        backend_reference={"collection": "generation-new"},
    )
    await async_session.refresh(old_generation)
    assert old_generation.status == "retired"
    assert old_generation.is_active is False
    assert new_generation.is_active is True


async def test_failed_shadow_and_document_keep_previous_active_state(async_session):
    user = User(username="runtime-doc-failure-owner", password="unused", is_active=True)  # noqa: S106
    kb = KnowledgeBaseRecord(name="failure-shared", user_id=user.id)
    async_session.add_all([user, kb])
    await async_session.flush()
    document, first, _ = await register_document(
        async_session,
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="policy.pdf",
        checksum=f"sha256:{'c' * 64}",
        mime_type="application/pdf",
        size_bytes=100,
        storage_path="runtime-documents/policy-v1.pdf",
    )
    await activate_document_version(async_session, document_id=document.id, version_id=first.id)
    document, second, _ = await register_document(
        async_session,
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="policy.pdf",
        checksum=f"sha256:{'d' * 64}",
        mime_type="application/pdf",
        size_bytes=120,
        storage_path="runtime-documents/policy-v2.pdf",
        duplicate_strategy="new_version",
    )
    generation, _ = await create_shadow_generation(
        async_session,
        knowledge_base_id=kb.id,
        fingerprint="sha256:retry",
    )

    await fail_document_version(
        async_session,
        document_id=document.id,
        version_id=second.id,
    )
    await fail_index_generation(async_session, generation_id=generation.id)
    await async_session.refresh(document)
    await async_session.refresh(generation)

    assert document.status == "active"
    assert first.status == "active"
    assert second.status == "failed"
    assert generation.status == "failed"

    retried, reset = await create_shadow_generation(
        async_session,
        knowledge_base_id=kb.id,
        fingerprint="sha256:retry",
    )
    assert reset is True
    assert retried.id == generation.id
    assert retried.status == "building"


async def test_expired_runtime_document_purges_vectors_raw_files_and_database(
    async_session,
    monkeypatch,
    tmp_path,
):
    now = datetime.now(timezone.utc)
    user = User(username="runtime-purge-owner", password="unused", is_active=True)  # noqa: S106
    kb = KnowledgeBaseRecord(name="purge-shared", user_id=user.id)
    async_session.add_all([user, kb])
    await async_session.flush()
    document, version, _ = await register_document(
        async_session,
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="expired.pdf",
        checksum=f"sha256:{'e' * 64}",
        mime_type="application/pdf",
        size_bytes=100,
        storage_path="runtime-documents/expired.pdf",
    )
    await activate_document_version(async_session, document_id=document.id, version_id=version.id)
    generation, _ = await create_shadow_generation(
        async_session,
        knowledge_base_id=kb.id,
        fingerprint="sha256:purge",
    )
    await activate_index_generation(
        async_session,
        generation=generation,
        backend_reference={"alias": "purge-shared--generation"},
    )
    await move_document_to_trash(async_session, document=document, retention_days=30)
    document.purge_after = now - timedelta(minutes=1)
    async_session.add(document)
    await async_session.commit()

    backend = MagicMock()
    backend.delete_by = AsyncMock()
    backend.teardown = AsyncMock()
    storage = MagicMock()
    storage.delete_file = AsyncMock()
    monkeypatch.setattr("langflow.services.runtime_document.create_backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(
        "langflow.services.runtime_document.KBStorageHelper.get_root_path",
        lambda: tmp_path,
    )

    purged = await purge_expired_runtime_documents(
        async_session,
        storage_service=storage,
        now=now,
    )
    await async_session.commit()

    assert purged == 1
    backend.delete_by.assert_awaited_once_with({"runtime_document_id": str(document.id)})
    backend.teardown.assert_awaited_once()
    storage.delete_file.assert_awaited_once_with("runtime-documents", "expired.pdf")
    assert await async_session.get(RuntimeDocument, document.id) is None
    assert (await async_session.exec(select(DocumentVersion).where(DocumentVersion.id == version.id))).first() is None
    audit = (
        await async_session.exec(
            select(RuntimeAuditEvent).where(
                RuntimeAuditEvent.event_type == "file.purged",
                RuntimeAuditEvent.resource_id == str(document.id),
            )
        )
    ).first()
    assert audit is not None
    assert audit.details == {"versions": 1, "index_generations": 1}


async def test_runtime_document_purge_retries_without_deleting_raw_file_when_vector_cleanup_fails(
    async_session,
    monkeypatch,
    tmp_path,
):
    now = datetime.now(timezone.utc)
    user = User(username="runtime-purge-retry", password="unused", is_active=True)  # noqa: S106
    kb = KnowledgeBaseRecord(name="purge-retry", user_id=user.id)
    document = RuntimeDocument(
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="retry.pdf",
        status="trash",
        purge_after=now - timedelta(minutes=1),
    )
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        checksum=f"sha256:{'f' * 64}",
        mime_type="application/pdf",
        size_bytes=100,
        storage_path="runtime-documents/retry.pdf",
        status="active",
    )
    generation = IndexGeneration(
        knowledge_base_id=kb.id,
        fingerprint="sha256:retry-purge",
        status="active",
        is_active=True,
        backend_reference={"alias": "purge-retry--generation"},
    )
    async_session.add_all([user, kb, document, version, generation])
    await async_session.commit()

    backend = MagicMock()
    backend.delete_by = AsyncMock(side_effect=RuntimeError("vector backend unavailable"))
    backend.teardown = AsyncMock()
    storage = MagicMock()
    storage.delete_file = AsyncMock()
    monkeypatch.setattr("langflow.services.runtime_document.create_backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(
        "langflow.services.runtime_document.KBStorageHelper.get_root_path",
        lambda: tmp_path,
    )

    with pytest.raises(RuntimeError, match="vector backend unavailable"):
        await purge_expired_runtime_documents(
            async_session,
            storage_service=storage,
            now=now,
        )

    backend.teardown.assert_awaited_once()
    storage.delete_file.assert_not_awaited()
    assert await async_session.get(RuntimeDocument, document.id) is not None
    assert await async_session.get(DocumentVersion, version.id) is not None
