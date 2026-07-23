"""Persistent state for reproducible on-premise deployment exports."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

JsonVariant = JSON().with_variant(JSONB(), "postgresql")


class DeploymentRelease(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "deployment_release"
    __table_args__ = (UniqueConstraint("user_id", "version", name="uq_deployment_release_user_version"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(
        sa_column=Column(sa.Uuid(), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    version: str = Field(index=True, nullable=False)
    agent_flow_version_id: UUID = Field(
        sa_column=Column(sa.Uuid(), ForeignKey("flow_version.id", ondelete="RESTRICT"), nullable=False, index=True)
    )
    ingestion_flow_version_id: UUID = Field(
        sa_column=Column(sa.Uuid(), ForeignKey("flow_version.id", ondelete="RESTRICT"), nullable=False, index=True)
    )
    subflow_version_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JsonVariant, nullable=False),
    )
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    manifest: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    api_version: str = Field(default="v1", nullable=False)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
    )


class DeploymentBuild(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "deployment_build"
    __table_args__ = (UniqueConstraint("release_id", "architecture", name="uq_deployment_build_release_arch"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    release_id: UUID = Field(
        sa_column=Column(
            sa.Uuid(), ForeignKey("deployment_release.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    architecture: str = Field(nullable=False, index=True)
    status: str = Field(default="pending", nullable=False, index=True)
    worker_job_id: str | None = Field(default=None, nullable=True, index=True)
    logs: str = Field(default="", sa_column=Column(Text, nullable=False))
    scan_report: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    critical_override_reason: str | None = Field(default=None, nullable=True)
    overridden_by_user_id: UUID | None = Field(
        default=None,
        sa_column=Column(sa.Uuid(), ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False),
    )


class DeploymentArtifact(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "deployment_artifact"
    __table_args__ = (UniqueConstraint("build_id", "artifact_type", name="uq_deployment_artifact_build_type"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    build_id: UUID = Field(
        sa_column=Column(sa.Uuid(), ForeignKey("deployment_build.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    artifact_type: str = Field(nullable=False)
    location: str = Field(nullable=False)
    digest: str = Field(nullable=False, index=True)
    size_bytes: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    checksums: dict[str, str] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    sbom: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    signature: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    pinned: bool = Field(default=False, nullable=False)
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )


class DeploymentAcceptanceTest(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "deployment_acceptance_test"
    __table_args__ = (UniqueConstraint("release_id", "name", name="uq_deployment_acceptance_test_release_name"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    release_id: UUID = Field(
        sa_column=Column(
            sa.Uuid(), ForeignKey("deployment_release.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    name: str = Field(nullable=False)
    required: bool = Field(default=True, nullable=False)
    request: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    expected: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JsonVariant, nullable=False))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
