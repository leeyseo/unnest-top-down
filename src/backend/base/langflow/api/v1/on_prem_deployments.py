"""Create and inspect immutable on-premise deployment releases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from langflow.api.utils import CurrentActiveUser, DbSession, DbSessionReadOnly
from langflow.api.v1.schemas.on_prem_deployments import (
    AcceptanceTestCreate,
    CriticalOverrideRequest,
    DeploymentArtifactRead,
    DeploymentBuildListResponse,
    DeploymentBuildRead,
    OnPremReleaseCreateRequest,
    OnPremReleaseListResponse,
    OnPremReleaseRead,
    OnPremReleaseValidationResponse,
)
from langflow.services.database.models.auth import AuthzAuditLog
from langflow.services.database.models.deployment_release import (
    DeploymentAcceptanceTest,
    DeploymentArtifact,
    DeploymentBuild,
    DeploymentRelease,
    DeploymentReleaseFlowVersion,
)
from langflow.services.database.models.flow_version.crud import get_flow_version_entries_by_ids
from langflow.services.deployment import (
    BuildKitWorkerClient,
    WorkerBuildStatus,
    analyze_release,
    sanitize_flow_for_build,
)

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


async def _owned_release(
    session: DbSession | DbSessionReadOnly,
    *,
    release_id: UUID,
    user_id: UUID,
) -> DeploymentRelease:
    release = (
        await session.exec(
            select(DeploymentRelease).where(
                DeploymentRelease.id == release_id,
                DeploymentRelease.user_id == user_id,
            )
        )
    ).first()
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Release not found")
    return release


async def _owned_build(
    session: DbSession | DbSessionReadOnly,
    *,
    release_id: UUID,
    build_id: UUID,
    user_id: UUID,
) -> tuple[DeploymentRelease, DeploymentBuild]:
    release = await _owned_release(session, release_id=release_id, user_id=user_id)
    build = (
        await session.exec(
            select(DeploymentBuild).where(
                DeploymentBuild.id == build_id,
                DeploymentBuild.release_id == release.id,
            )
        )
    ).first()
    if build is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build not found")
    return release, build


async def _build_read(session: DbSession | DbSessionReadOnly, build: DeploymentBuild) -> DeploymentBuildRead:
    artifacts = (
        await session.exec(select(DeploymentArtifact).where(DeploymentArtifact.build_id == build.id))
    ).all()
    return DeploymentBuildRead(
        id=build.id,
        release_id=build.release_id,
        architecture=build.architecture,
        status=build.status,
        worker_job_id=build.worker_job_id,
        logs=build.logs,
        scan_report=build.scan_report,
        critical_override_reason=build.critical_override_reason,
        artifacts=[
            DeploymentArtifactRead.model_validate(artifact, from_attributes=True) for artifact in artifacts
        ],
    )


def _worker_client_or_503() -> BuildKitWorkerClient:
    try:
        return BuildKitWorkerClient.from_env()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


async def _apply_worker_status(
    session: DbSession,
    *,
    release: DeploymentRelease,
    build: DeploymentBuild,
    worker: WorkerBuildStatus,
) -> None:
    if (
        worker.status == "succeeded"
        and release.manifest.get("build", {}).get("signing_enabled")
        and any(not artifact.signature for artifact in worker.artifacts)
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="BuildKit worker returned an unsigned artifact for a signed release",
        )
    build.worker_job_id = worker.job_id
    build.status = worker.status
    build.logs = worker.logs
    build.scan_report = worker.scan_report
    session.add(build)
    if worker.status != "succeeded":
        return
    existing = (
        await session.exec(select(DeploymentArtifact).where(DeploymentArtifact.build_id == build.id))
    ).all()
    for artifact in existing:
        await session.delete(artifact)
    if existing:
        await session.flush()
    pinned = bool(release.config.get("retention", {}).get("pinned"))
    retention_days = int(release.config.get("retention", {}).get("days", 30))
    expires_at = None if pinned else datetime.now(timezone.utc) + timedelta(days=retention_days)
    session.add_all(
        [
            DeploymentArtifact(
                build_id=build.id,
                artifact_type=artifact.artifact_type,
                location=artifact.location,
                digest=artifact.digest,
                size_bytes=artifact.size_bytes,
                checksums=artifact.checksums,
                sbom=artifact.sbom,
                signature=artifact.signature,
                pinned=pinned,
                expires_at=expires_at,
            )
            for artifact in worker.artifacts
        ]
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

    attached_versions = {
        payload.agent_flow_version_id: "agent",
        payload.ingestion_flow_version_id: "ingestion",
        **dict.fromkeys(analysis.subflow_version_ids, "subflow"),
    }
    session.add_all(
        [
            DeploymentReleaseFlowVersion(
                release_id=release.id,
                flow_version_id=version_id,
                role=role,
            )
            for version_id, role in attached_versions.items()
        ]
    )
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
    release = await _owned_release(session, release_id=release_id, user_id=current_user.id)
    return _to_read(release)


@router.post("/{release_id}/builds/{build_id}/submit", response_model=DeploymentBuildRead)
async def submit_build(
    release_id: UUID,
    build_id: UUID,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> DeploymentBuildRead:
    release, build = await _owned_build(
        session,
        release_id=release_id,
        build_id=build_id,
        user_id=current_user.id,
    )
    if build.status in {"queued", "running", "succeeded"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Build is already {build.status}")

    version_ids = [
        release.agent_flow_version_id,
        release.ingestion_flow_version_id,
        *(UUID(value) for value in release.subflow_version_ids),
    ]
    versions = await get_flow_version_entries_by_ids(session, version_ids, current_user.id)
    if missing := [str(version_id) for version_id in version_ids if version_id not in versions]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Release Flow Versions are no longer available: {', '.join(missing)}",
        )
    payload = {
        "release_id": str(release.id),
        "build_id": str(build.id),
        "manifest": release.manifest,
        "flows": [
            {
                "version_id": str(version_id),
                "data": sanitize_flow_for_build(versions[version_id].data or {}),
            }
            for version_id in version_ids
        ],
        "critical_override": (
            {"reason": build.critical_override_reason, "user_id": str(build.overridden_by_user_id)}
            if build.critical_override_reason
            else None
        ),
        "reproducible": {"source_date_epoch": 0, "sort_files": True},
    }
    try:
        async with _worker_client_or_503() as client:
            worker = await client.submit(payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="BuildKit worker request failed") from exc
    await _apply_worker_status(session, release=release, build=build, worker=worker)
    await session.flush()
    return await _build_read(session, build)


@router.get("/{release_id}/builds", response_model=DeploymentBuildListResponse)
async def list_builds(
    release_id: UUID,
    session: DbSessionReadOnly,
    current_user: CurrentActiveUser,
) -> DeploymentBuildListResponse:
    release = await _owned_release(session, release_id=release_id, user_id=current_user.id)
    builds = (
        await session.exec(
            select(DeploymentBuild)
            .where(DeploymentBuild.release_id == release.id)
            .order_by(col(DeploymentBuild.created_at).desc())
        )
    ).all()
    return DeploymentBuildListResponse(builds=[await _build_read(session, build) for build in builds])


@router.post("/{release_id}/builds/{build_id}/sync", response_model=DeploymentBuildRead)
async def sync_build(
    release_id: UUID,
    build_id: UUID,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> DeploymentBuildRead:
    release, build = await _owned_build(
        session,
        release_id=release_id,
        build_id=build_id,
        user_id=current_user.id,
    )
    if not build.worker_job_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Build has not been submitted")
    try:
        async with _worker_client_or_503() as client:
            worker = await client.get(build.worker_job_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="BuildKit worker request failed") from exc
    await _apply_worker_status(session, release=release, build=build, worker=worker)
    await session.flush()
    return await _build_read(session, build)


@router.patch("/{release_id}/builds/{build_id}/critical-override", response_model=DeploymentBuildRead)
async def override_critical_build_block(
    release_id: UUID,
    build_id: UUID,
    payload: CriticalOverrideRequest,
    session: DbSession,
    current_user: CurrentActiveUser,
) -> DeploymentBuildRead:
    _release, build = await _owned_build(
        session,
        release_id=release_id,
        build_id=build_id,
        user_id=current_user.id,
    )
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin access required")
    if build.status != "blocked":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a blocked build can be overridden")
    build.critical_override_reason = payload.reason
    build.overridden_by_user_id = current_user.id
    build.status = "pending"
    session.add(build)
    session.add(
        AuthzAuditLog(
            user_id=current_user.id,
            action="deployment:critical_override",
            resource_type="deployment_build",
            resource_id=build.id,
            result="allow",
            details={"reason": payload.reason, "release_id": str(release_id)},
        )
    )
    await session.flush()
    return await _build_read(session, build)
