from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.user.model import User
from langflow.services.runtime_document import (
    activate_document_version,
    activate_index_generation,
    create_shadow_generation,
    fail_document_version,
    fail_index_generation,
    register_document,
)


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
