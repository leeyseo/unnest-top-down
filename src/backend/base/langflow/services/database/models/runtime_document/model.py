"""Runtime document and shadow-index lifecycle state."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import JSON, BigInteger, CheckConstraint, Column, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

JsonVariant = JSON().with_variant(JSONB(), "postgresql")


class RuntimeDocument(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "runtime_document"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "name", name="uq_runtime_document_kb_name"),
        CheckConstraint("status IN ('pending', 'active', 'failed', 'trash')", name="ck_runtime_document_status"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(
        sa_column=Column(sa.Uuid(), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    knowledge_base_id: UUID = Field(
        sa_column=Column(
            sa.Uuid(), ForeignKey("knowledge_base.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    name: str = Field(nullable=False)
    status: str = Field(default="pending", nullable=False, index=True)
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    purge_after: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )


class DocumentVersion(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "document_version"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        CheckConstraint(
            "status IN ('pending', 'active', 'superseded', 'failed')",
            name="ck_document_version_status",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    document_id: UUID = Field(
        sa_column=Column(sa.Uuid(), ForeignKey("runtime_document.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    version_number: int = Field(nullable=False, ge=1)
    checksum: str = Field(nullable=False, index=True)
    mime_type: str = Field(nullable=False)
    size_bytes: int = Field(sa_column=Column(BigInteger, nullable=False))
    storage_path: str = Field(nullable=False)
    document_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    status: str = Field(default="pending", nullable=False, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )


class IndexGeneration(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "index_generation"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "fingerprint", name="uq_index_generation_kb_fingerprint"),
        CheckConstraint(
            "status IN ('building', 'ready', 'active', 'failed', 'retired')",
            name="ck_index_generation_status",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    knowledge_base_id: UUID = Field(
        sa_column=Column(
            sa.Uuid(), ForeignKey("knowledge_base.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    fingerprint: str = Field(nullable=False)
    status: str = Field(default="building", nullable=False, index=True)
    is_active: bool = Field(default=False, nullable=False, index=True)
    backend_reference: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    activated_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
