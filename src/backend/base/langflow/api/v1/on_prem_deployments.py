"""Create and inspect immutable on-premise deployment releases."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from langflow.api.utils import CurrentActiveUser, DbSession, DbSessionReadOnly
from langflow.api.v1.schemas.on_prem_deployments import (
    AcceptanceTestCreate,
    OnPremReleaseCreateRequest,
    OnPremReleaseListResponse,
    OnPremReleaseRead,
    OnPremReleaseValidationResponse,
)
from langflow.services.database.models.deployment_release import (
    DeploymentAcceptanceTest,
    DeploymentBuild,
    DeploymentRelease,
)
from langflow.services.deployment import analyze_release

router = APIRouter(prefix="/deployments/on-prem/releases", tags=["On-premise Deployments"])


def _to_read(release: DeploymentRelease, *, warnings: list[str] | None = None) -> OnPremReleaseRead:
    return OnPremReleaseRead(
        id=release.id,
        release_version=release.version,
        api_version=release.api_version,
        agent_flow_version_id=release.agent_flow_version_id,
        ingestion_flow_version_id=release.ingestion_flow_version_id,
        subflow_version_ids=[UUID(value) for value in release.subflow_version_ids],
        config=release.config,
        manifest=release.manifest,
        warnings=warnings or [],
    )


async def _latest_manifest(session: DbSession | DbSessionReadOnly, user_id: UUID) -> dict | None:
    latest = (
        await session.exec(
            select(DeploymentRelease)
            .where(DeploymentRelease.user_id == user_id)
            .order_by(col(DeploymentRelease.created_at).desc())
        )
    ).first()
    return latest.manifest if latest else None


async def _analyze(
    session: DbSession | DbSessionReadOnly,
    current_user: CurrentActiveUser,
    payload: OnPremReleaseCreateRequest,
):
    return await analyze_release(
        session,
        user_id=current_user.id,
        release_version=payload.release_version,
        agent_flow_version_id=payload.agent_flow_version_id,
        ingestion_flow_version_id=payload.ingestion_flow_version_id,
        config=payload.config,
        api=payload.api,
        previous_manifest=await _latest_manifest(session, current_user.id),
    )


@router.post("/validate", response_model=OnPremReleaseValidationResponse)
async def validate_release(
    payload: OnPremReleaseCreateRequest,
    session: DbSessionReadOnly,
    current_user: CurrentActiveUser,
) -> OnPremReleaseValidationResponse:
    analysis = await _analyze(session, current_user, payload)
    return OnPremReleaseValidationResponse(
        manifest=analysis.manifest,
        errors=list(analysis.errors),
        warnings=list(analysis.warnings),
    )


@router.post("", response_model=OnPremReleaseRead, status_code=status.HTTP_201_CREATED)
async def create_release(
    payload: OnPremReleaseCreateRequest,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> OnPremReleaseRead:
    existing = (
        await session.exec(
            select(DeploymentRelease).where(
                DeploymentRelease.user_id == current_user.id,
                DeploymentRelease.version == payload.release_version,
            )
        )
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Release version already exists")

    analysis = await _analyze(session, current_user, payload)
    if analysis.errors or analysis.manifest is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"errors": list(analysis.errors), "warnings": list(analysis.warnings)},
        )

    release = DeploymentRelease(
        user_id=current_user.id,
        version=payload.release_version,
        agent_flow_version_id=payload.agent_flow_version_id,
        ingestion_flow_version_id=payload.ingestion_flow_version_id,
        subflow_version_ids=[str(value) for value in analysis.subflow_version_ids],
        config=payload.config.model_dump(mode="json"),
        manifest=analysis.manifest,
        api_version=analysis.manifest["api"]["version"],
    )
    session.add(release)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Release version already exists") from exc

    session.add(
        DeploymentBuild(
            release_id=release.id,
            architecture=payload.config.architecture,
            status="pending",
        )
    )
    tests = payload.acceptance_tests or [
        AcceptanceTestCreate(name="health", request={"path": "/health"}, expected={"status": 200}),
        AcceptanceTestCreate(
            name="agent-smoke",
            request={
                "path": f"/api/{release.api_version}/agent/run",
                "body": payload.api.request_example,
            },
            expected={"status": 200, "body": payload.api.response_example},
        ),
    ]
    session.add_all(
        [
            DeploymentAcceptanceTest(
                release_id=release.id,
                name=test.name,
                required=test.required,
                request=test.request,
                expected=test.expected,
            )
            for test in tests
        ]
    )
    await session.flush()
    return _to_read(release, warnings=list(analysis.warnings))


@router.get("", response_model=OnPremReleaseListResponse)
async def list_releases(
    session: DbSessionReadOnly,
    current_user: CurrentActiveUser,
) -> OnPremReleaseListResponse:
    releases = (
        await session.exec(
            select(DeploymentRelease)
            .where(DeploymentRelease.user_id == current_user.id)
            .order_by(col(DeploymentRelease.created_at).desc())
            .limit(100)
        )
    ).all()
    return OnPremReleaseListResponse(releases=[_to_read(release) for release in releases])


@router.get("/{release_id}", response_model=OnPremReleaseRead)
async def get_release(
    release_id: UUID,
    session: DbSessionReadOnly,
    current_user: CurrentActiveUser,
) -> OnPremReleaseRead:
    release = (
        await session.exec(
            select(DeploymentRelease).where(
                DeploymentRelease.id == release_id,
                DeploymentRelease.user_id == current_user.id,
            )
        )
    ).first()
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    return _to_read(release)
