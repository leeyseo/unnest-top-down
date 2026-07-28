"""Tamper-evident audit records for the isolated on-premise runtime."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import BigInteger, Column, DateTime, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import JSON, Field, SQLModel

JsonVariant = JSON().with_variant(JSONB(), "postgresql")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeAuditEvent(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "runtime_audit_event"
    __table_args__ = (
        UniqueConstraint("sequence", name="uq_runtime_audit_event_sequence"),
        UniqueConstraint("event_hash", name="uq_runtime_audit_event_hash"),
        Index("ix_runtime_audit_event_occurred_at", "occurred_at"),
        Index("ix_runtime_audit_event_actor", "actor_user_id", "occurred_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    previous_hash: str = Field(nullable=False, max_length=64)
    event_hash: str = Field(nullable=False, max_length=64)
    event_type: str = Field(nullable=False, index=True)
    actor_user_id: UUID | None = Field(default=None, sa_column=Column(sa.Uuid(), nullable=True))
    resource_type: str | None = Field(default=None, nullable=True)
    resource_id: str | None = Field(default=None, nullable=True)
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    occurred_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class RuntimeAuditCheckpoint(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "runtime_audit_checkpoint"
    __table_args__ = (Index("ix_runtime_audit_checkpoint_sequence", "event_sequence"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_sequence: int = Field(sa_column=Column(BigInteger, nullable=False))
    event_hash: str = Field(nullable=False, max_length=64)
    signature: str = Field(sa_column=Column(Text, nullable=False))
    public_key: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
