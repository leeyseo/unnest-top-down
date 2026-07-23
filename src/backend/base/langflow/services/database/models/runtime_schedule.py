"""Persisted cron schedules for the isolated on-premise runtime."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import JSON, Field, SQLModel

JsonVariant = JSON().with_variant(JSONB(), "postgresql")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeSchedule(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "runtime_schedule"
    __table_args__ = (UniqueConstraint("name", name="uq_runtime_schedule_name"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(nullable=False)
    cron_expression: str = Field(nullable=False)
    timezone: str = Field(default="UTC", nullable=False)
    api_version: str = Field(default="v1", nullable=False)
    request_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    enabled: bool = Field(default=True, nullable=False, index=True)
    next_run_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))
    last_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_finished_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_status: str | None = Field(default=None, nullable=True)
    last_error: str | None = Field(default=None, nullable=True, max_length=500)
    created_by_user_id: UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utc_now,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
