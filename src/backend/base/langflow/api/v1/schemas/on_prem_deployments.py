"""Control-plane contracts for an Unnest on-premise release export."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, HttpUrl, field_validator, model_validator

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _validate_semver(value: str) -> str:
    normalized = value.strip()
    if not _SEMVER_RE.fullmatch(normalized):
        msg = "release_version must be a valid SemVer value (for example, 1.2.3)"
        raise ValueError(msg)
    return normalized


SemVer = Annotated[str, AfterValidator(_validate_semver)]


class BrandingConfig(BaseModel):
    model_config = {"extra": "forbid"}

    solution_name: str = "Unnest"
    organization_name: str = ""
    logo_url: HttpUrl | None = None
    primary_color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    login_notice: str = ""
    show_unnest_branding: bool = True

    @field_validator("logo_url")
    @classmethod
    def safe_logo_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and (value.username or value.password or value.query or value.fragment):
            msg = "logo_url must not contain credentials, query parameters, or a fragment"
            raise ValueError(msg)
        return value


class FeatureConfig(BaseModel):
    model_config = {"extra": "forbid"}

    signing: bool = True
    backups: bool = True
    monitoring: bool = True
    clamav: bool = False
    grafana: bool = False


class ArtifactRetentionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    days: int = Field(default=30, ge=1)
    max_bytes: int = Field(default=20 * 1024**3, ge=1)
    pinned: bool = False


class ResourceConfig(BaseModel):
    model_config = {"extra": "forbid"}

    cpu: float = Field(default=2, gt=0)
    memory_bytes: int = Field(default=4 * 1024**3, ge=512 * 1024**2)
    disk_bytes: int = Field(default=20 * 1024**3, ge=1024**3)
    gpu_count: int = Field(default=0, ge=0)


class OnPremDeploymentConfig(BaseModel):
    model_config = {"extra": "forbid"}

    orchestrator: Literal["compose", "helm"] = "compose"
    topology: Literal["single", "ha"] = "single"
    architecture: Literal["amd64", "arm64"] = "amd64"
    accelerator: Literal["cpu", "nvidia", "amd"] = "cpu"
    database: Literal["embedded-postgresql", "external-postgresql"] = "embedded-postgresql"
    storage: Literal["local", "minio", "s3"] = "local"
    infrastructure: Literal["bundled", "external"] = "bundled"
    model_runtime: Literal["external", "bundled"] = "external"
    include_model_weights: bool = False
    store_conversations: bool = False
    conversation_retention_days: int | None = Field(default=None, ge=1)
    tls: Literal["institution", "self-signed"] = "institution"
    default_language: Literal["ko", "en"] = "ko"
    allow_language_switch: bool = True
    external_endpoints: list[HttpUrl] = Field(default_factory=list)
    additional_secret_names: list[str] = Field(default_factory=list)
    base_image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    retention: ArtifactRetentionConfig = Field(default_factory=ArtifactRetentionConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)

    @field_validator("external_endpoints")
    @classmethod
    def safe_external_endpoints(cls, values: list[HttpUrl]) -> list[HttpUrl]:
        if any(value.username or value.password or value.query or value.fragment for value in values):
            msg = "external_endpoints must not contain credentials, query parameters, or fragments"
            raise ValueError(msg)
        return list(dict.fromkeys(values))

    @field_validator("additional_secret_names")
    @classmethod
    def valid_secret_names(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in normalized):
            msg = "additional_secret_names must contain valid environment variable names"
            raise ValueError(msg)
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_profile(self) -> OnPremDeploymentConfig:
        if self.topology == "ha" and self.orchestrator != "helm":
            msg = "HA topology requires the Helm orchestrator"
            raise ValueError(msg)
        if self.accelerator == "cpu" and self.resources.gpu_count:
            msg = "gpu_count must be zero for the CPU accelerator"
            raise ValueError(msg)
        if self.store_conversations and self.conversation_retention_days is None:
            msg = "conversation_retention_days is required when conversation storage is enabled"
            raise ValueError(msg)
        if not self.store_conversations and self.conversation_retention_days is not None:
            msg = "conversation_retention_days must be omitted when conversation storage is disabled"
            raise ValueError(msg)
        return self


class AgentApiContract(BaseModel):
    model_config = {"extra": "forbid"}

    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    request_example: dict[str, Any]
    response_example: Any


class AcceptanceTestCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    required: bool = True
    request: dict[str, Any]
    expected: dict[str, Any]


class OnPremReleaseCreateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    release_version: SemVer
    agent_flow_version_id: UUID
    ingestion_flow_version_id: UUID
    config: OnPremDeploymentConfig = Field(default_factory=OnPremDeploymentConfig)
    api: AgentApiContract
    acceptance_tests: list[AcceptanceTestCreate] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def distinct_root_flows(self) -> OnPremReleaseCreateRequest:
        if self.agent_flow_version_id == self.ingestion_flow_version_id:
            msg = "Agent and ingestion flow versions must be different"
            raise ValueError(msg)
        return self


class OnPremReleaseRead(BaseModel):
    id: UUID
    release_version: str
    api_version: str
    agent_flow_version_id: UUID
    ingestion_flow_version_id: UUID
    subflow_version_ids: list[UUID]
    config: dict[str, Any]
    manifest: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class OnPremReleaseListResponse(BaseModel):
    releases: list[OnPremReleaseRead]


class OnPremReleaseValidationResponse(BaseModel):
    manifest: dict[str, Any] | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DeploymentArtifactRead(BaseModel):
    artifact_type: str
    location: str
    digest: str
    size_bytes: int
    checksums: dict[str, str]
    sbom: dict[str, Any]
    signature: str | None
    expires_at: datetime | None


class DeploymentBuildRead(BaseModel):
    id: UUID
    release_id: UUID
    architecture: str
    status: str
    worker_job_id: str | None
    logs: str
    scan_report: dict[str, Any]
    critical_override_reason: str | None
    artifacts: list[DeploymentArtifactRead] = Field(default_factory=list)


class DeploymentBuildListResponse(BaseModel):
    builds: list[DeploymentBuildRead]


class CriticalOverrideRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=10, max_length=2000)
