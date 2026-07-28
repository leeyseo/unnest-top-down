"""Transactional lifecycle helpers for runtime documents and indexes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from lfx.base.knowledge_bases.backends import create_backend
from sqlmodel import col, func, select, update

from langflow.api.utils.kb_helpers import KBStorageHelper
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.runtime_document import (
    DocumentVersion,
    IndexGeneration,
    RuntimeDocument,
)
from langflow.services.database.models.user.model import User
from langflow.services.runtime_audit import append_runtime_audit_event

if TYPE_CHECKING:
    from uuid import UUID

    from sqlmodel.ext.asyncio.session import AsyncSession

    from langflow.services.storage.service import StorageService

DuplicateStrategy = Literal["skip", "new_version", "replace"]
SHA256_DIGEST_LENGTH = 71


async def register_document(
    session: AsyncSession,
    *,
    user_id: UUID,
    knowledge_base_id: UUID,
    name: str,
    checksum: str,
    mime_type: str,
    size_bytes: int,
    storage_path: str,
    document_metadata: dict[str, Any] | None = None,
    duplicate_strategy: DuplicateStrategy = "skip",
) -> tuple[RuntimeDocument, DocumentVersion, bool]:
    """Create a pending version, or return the existing duplicate for ``skip``."""
    if not checksum.startswith("sha256:") or len(checksum) != SHA256_DIGEST_LENGTH:
        msg = "checksum must be a sha256 digest"
        raise ValueError(msg)
    document = (
        await session.exec(
            select(RuntimeDocument).where(
                RuntimeDocument.user_id == user_id,
                RuntimeDocument.knowledge_base_id == knowledge_base_id,
                RuntimeDocument.name == name,
            )
        )
    ).first()
    duplicate = (
        await session.exec(
            select(DocumentVersion)
            .join(RuntimeDocument, RuntimeDocument.id == DocumentVersion.document_id)
            .where(
                RuntimeDocument.user_id == user_id,
                RuntimeDocument.knowledge_base_id == knowledge_base_id,
                DocumentVersion.checksum == checksum,
            )
            .order_by(col(DocumentVersion.created_at).desc())
        )
    ).first()
    if duplicate is not None and duplicate_strategy == "skip":
        duplicate_document = await session.get(RuntimeDocument, duplicate.document_id)
        if duplicate_document is None:
            msg = "Duplicate document row is unavailable"
            raise RuntimeError(msg)
        return duplicate_document, duplicate, False

    if document is None:
        document = RuntimeDocument(
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            name=name,
        )
        session.add(document)
        await session.flush()
        version_number = 1
    else:
        current_max = (
            await session.exec(
                select(func.max(DocumentVersion.version_number)).where(
                    DocumentVersion.document_id == document.id
                )
            )
        ).one()
        version_number = int(current_max or 0) + 1
        document.status = "pending"
        document.deleted_at = None
        document.purge_after = None
        document.updated_at = datetime.now(timezone.utc)
        session.add(document)

    version = DocumentVersion(
        document_id=document.id,
        version_number=version_number,
        checksum=checksum,
        mime_type=mime_type,
        size_bytes=size_bytes,
        storage_path=storage_path,
        document_metadata=document_metadata or {},
    )
    session.add(version)
    await session.flush()
    return document, version, True


async def activate_document_version(
    session: AsyncSession,
    *,
    document_id: UUID,
    version_id: UUID,
) -> DocumentVersion:
    version = await session.get(DocumentVersion, version_id)
    document = await session.get(RuntimeDocument, document_id)
    if version is None or document is None or version.document_id != document.id:
        msg = "Document version not found"
        raise ValueError(msg)
    await session.exec(
        update(DocumentVersion)
        .where(DocumentVersion.document_id == document.id, DocumentVersion.id != version.id)
        .values(status="superseded")
    )
    version.status = "active"
    document.status = "active"
    document.updated_at = datetime.now(timezone.utc)
    session.add(version)
    session.add(document)
    await session.flush()
    return version


async def fail_document_version(
    session: AsyncSession,
    *,
    document_id: UUID,
    version_id: UUID,
) -> None:
    version = await session.get(DocumentVersion, version_id)
    document = await session.get(RuntimeDocument, document_id)
    if version is None or document is None or version.document_id != document.id:
        return
    version.status = "failed"
    previous_active = (
        await session.exec(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.id != version.id,
                DocumentVersion.status == "active",
            )
        )
    ).first()
    document.status = "active" if previous_active else "failed"
    document.updated_at = datetime.now(timezone.utc)
    session.add(version)
    session.add(document)
    await session.flush()


async def move_document_to_trash(
    session: AsyncSession,
    *,
    document: RuntimeDocument,
    retention_days: int,
) -> None:
    now = datetime.now(timezone.utc)
    document.status = "trash"
    document.deleted_at = now
    document.purge_after = now + timedelta(days=retention_days)
    document.updated_at = now
    session.add(document)
    await session.flush()


async def restore_document(session: AsyncSession, *, document: RuntimeDocument) -> None:
    active = (
        await session.exec(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.status == "active",
            )
        )
    ).first()
    if active is None:
        msg = "Document has no active version to restore"
        raise ValueError(msg)
    document.status = "active"
    document.deleted_at = None
    document.purge_after = None
    document.updated_at = datetime.now(timezone.utc)
    session.add(document)
    await session.flush()


async def purge_expired_runtime_documents(
    session: AsyncSession,
    *,
    storage_service: StorageService,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    documents = (
        await session.exec(
            select(RuntimeDocument).where(
                RuntimeDocument.status == "trash",
                col(RuntimeDocument.purge_after).is_not(None),
                col(RuntimeDocument.purge_after) <= current,
            )
        )
    ).all()
    purged = 0
    for document in documents:
        knowledge_base = await session.get(KnowledgeBaseRecord, document.knowledge_base_id)
        user = await session.get(User, document.user_id)
        if knowledge_base is None or user is None:
            msg = "Runtime document owner or Knowledge Base is unavailable"
            raise RuntimeError(msg)
        generations = (
            await session.exec(select(IndexGeneration).where(IndexGeneration.knowledge_base_id == knowledge_base.id))
        ).all()
        aliases = {
            alias
            for generation in generations
            if isinstance(generation.backend_reference, dict)
            and isinstance((alias := generation.backend_reference.get("alias")), str)
            and alias
        }
        user_root = (KBStorageHelper.get_root_path() / user.username).resolve()
        for alias in sorted(aliases):
            if Path(alias).name != alias or alias in {".", ".."}:
                msg = "Runtime index alias is invalid"
                raise RuntimeError(msg)
            backend_path = (user_root / alias).resolve()
            if user_root not in backend_path.parents:
                msg = "Runtime index alias escapes its Knowledge Base root"
                raise RuntimeError(msg)
            backend = create_backend(
                knowledge_base.backend_type,
                kb_name=alias,
                kb_path=backend_path,
                backend_config=knowledge_base.backend_config,
                user_id=document.user_id,
            )
            try:
                await backend.delete_by({"runtime_document_id": str(document.id)})
            finally:
                await backend.teardown()

        versions = (await session.exec(select(DocumentVersion).where(DocumentVersion.document_id == document.id))).all()
        for storage_path in sorted({version.storage_path for version in versions}):
            namespace, separator, storage_name = storage_path.partition("/")
            if not separator or not namespace or not storage_name:
                msg = "Runtime document storage path is invalid"
                raise RuntimeError(msg)
            await storage_service.delete_file(namespace, storage_name)
        await append_runtime_audit_event(
            session,
            event_type="file.purged",
            resource_type="runtime_document",
            resource_id=str(document.id),
            details={"versions": len(versions), "index_generations": len(aliases)},
        )
        for version in versions:
            await session.delete(version)
        await session.delete(document)
        purged += 1
    await session.flush()
    return purged


async def create_shadow_generation(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    fingerprint: str,
) -> tuple[IndexGeneration, bool]:
    existing = (
        await session.exec(
            select(IndexGeneration).where(
                IndexGeneration.knowledge_base_id == knowledge_base_id,
                IndexGeneration.fingerprint == fingerprint,
            )
        )
    ).first()
    if existing is not None:
        if existing.status in {"failed", "retired"}:
            existing.status = "building"
            existing.is_active = False
            existing.backend_reference = {}
            existing.activated_at = None
            session.add(existing)
            await session.flush()
            return existing, True
        return existing, False
    generation = IndexGeneration(knowledge_base_id=knowledge_base_id, fingerprint=fingerprint)
    session.add(generation)
    await session.flush()
    return generation, True


async def fail_index_generation(
    session: AsyncSession,
    *,
    generation_id: UUID,
) -> None:
    generation = await session.get(IndexGeneration, generation_id)
    if generation is None or generation.is_active:
        return
    generation.status = "failed"
    session.add(generation)
    await session.flush()


async def activate_index_generation(
    session: AsyncSession,
    *,
    generation: IndexGeneration,
    backend_reference: dict[str, Any],
) -> None:
    await session.exec(
        update(IndexGeneration)
        .where(
            IndexGeneration.knowledge_base_id == generation.knowledge_base_id,
            IndexGeneration.id != generation.id,
            IndexGeneration.is_active.is_(True),
        )
        .values(is_active=False, status="retired")
    )
    generation.backend_reference = backend_reference
    generation.status = "active"
    generation.is_active = True
    generation.activated_at = datetime.now(timezone.utc)
    session.add(generation)
    await session.flush()
