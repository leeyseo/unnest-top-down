"""Persistent bootstrap state for the isolated on-premise runtime."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Column, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import JSON, Field, SQLModel

JsonVariant = JSON().with_variant(JSONB(), "postgresql")


class RuntimeConfiguration(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "runtime_configuration"

    id: int = Field(default=1, primary_key=True)
    setup_complete: bool = Field(default=False, nullable=False)
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    encrypted_secrets: str = Field(nullable=False)
    master_key_fingerprint: str = Field(nullable=False, max_length=64)
    created_by_user_id: UUID = Field(
        sa_column=Column(ForeignKey("user.id", ondelete="RESTRICT"), nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
