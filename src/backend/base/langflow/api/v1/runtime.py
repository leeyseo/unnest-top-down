"""Minimal API surface for an exported on-premise runtime."""

from __future__ import annotations

import copy
import os
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import col, select

from langflow.api.utils import CurrentActiveUser, DbSessionReadOnly
from langflow.api.v1.endpoints import _run_flow_internal
from langflow.api.v1.schemas import SimplifiedAPIRequest
from langflow.services.auth.utils import get_current_active_user
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow, FlowRead
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.message.model import MessageTable
from langflow.services.database.models.user.model import User

router = APIRouter(tags=["Runtime"])


def _setup_complete() -> bool:
    # ponytail: replace this process-level gate when encrypted runtime settings land.
    return os.getenv("UNNEST_RUNTIME_SETUP_COMPLETE", "").lower() in {"1", "true", "yes", "on"}


async def _release_for_api(session: DbSessionReadOnly, api_version: str | None = None) -> DeploymentRelease:
    if not _setup_complete():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Initial setup is incomplete")
    statement = select(DeploymentRelease)
    if api_version is not None:
        statement = statement.where(DeploymentRelease.api_version == api_version)
    release = (await session.exec(statement.order_by(col(DeploymentRelease.created_at).desc()))).first()
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime API version not found")
    return release


async def _immutable_agent_flow(
    session: DbSessionReadOnly,
    api_version: str,
) -> tuple[DeploymentRelease, FlowRead]:
    release = await _release_for_api(session, api_version)
    version = (
        await session.exec(select(FlowVersion).where(FlowVersion.id == release.agent_flow_version_id))
    ).first()
    flow = (await session.exec(select(Flow).where(Flow.id == version.flow_id))).first() if version else None
    if version is None or flow is None or not isinstance(version.data, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Immutable Agent Flow Version is unavailable",
        )
    immutable = FlowRead.model_validate(flow, from_attributes=True).model_copy(
        update={"data": copy.deepcopy(version.data)}
    )
    return release, immutable


async def _run_agent(
    *,
    api_version: str,
    stream: bool,
    background_tasks: BackgroundTasks,
    input_request: SimplifiedAPIRequest | None,
    current_user: User,
    session: DbSessionReadOnly,
    http_request: Request,
):
    release, flow = await _immutable_agent_flow(session, api_version)
    # The route accepts no flow identifier: successful authentication grants
    # execution only of the release-pinned Agent version.
    return await _run_flow_internal(
        background_tasks=background_tasks,
        flow=flow,
        input_request=input_request,
        stream=stream,
        api_key_user=current_user,
        context={"deployment_release_id": str(release.id)},
        http_request=http_request,
    )


@router.get("/ready")
async def ready(session: DbSessionReadOnly) -> dict[str, str]:
    release = await _release_for_api(session)
    return {"status": "ok", "release_version": release.version}


@router.post("/api/{api_version}/agent/run", response_model=None)
async def run_agent(
    api_version: str,
    background_tasks: BackgroundTasks,
    current_user: CurrentActiveUser,
    session: DbSessionReadOnly,
    http_request: Request,
    input_request: SimplifiedAPIRequest | None = None,
):
    return await _run_agent(
        api_version=api_version,
        stream=False,
        background_tasks=background_tasks,
        input_request=input_request,
        current_user=current_user,
        session=session,
        http_request=http_request,
    )


@router.post("/api/{api_version}/agent/stream", response_model=None)
async def stream_agent(
    api_version: str,
    background_tasks: BackgroundTasks,
    current_user: CurrentActiveUser,
    session: DbSessionReadOnly,
    http_request: Request,
    input_request: SimplifiedAPIRequest | None = None,
):
    return await _run_agent(
        api_version=api_version,
        stream=True,
        background_tasks=background_tasks,
        input_request=input_request,
        current_user=current_user,
        session=session,
        http_request=http_request,
    )


@router.post("/api/{api_version}/webhooks/{name}", response_model=None)
async def run_webhook(
    api_version: str,
    name: str,  # noqa: ARG001 - named hooks share the release-pinned Agent
    background_tasks: BackgroundTasks,
    current_user: CurrentActiveUser,
    session: DbSessionReadOnly,
    http_request: Request,
    input_request: SimplifiedAPIRequest | None = None,
):
    return await _run_agent(
        api_version=api_version,
        stream=False,
        background_tasks=background_tasks,
        input_request=input_request,
        current_user=current_user,
        session=session,
        http_request=http_request,
    )


@router.get("/api/{api_version}/sessions")
async def list_sessions(
    api_version: str,
    session: DbSessionReadOnly,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> list[str]:
    _release, flow = await _immutable_agent_flow(session, api_version)
    statement = select(MessageTable.session_id).where(MessageTable.flow_id == flow.id).distinct()
    if not current_user.is_superuser:
        statement = statement.where(
            MessageTable.session_metadata["user_id"].as_string() == str(current_user.id)  # type: ignore[index]
        )
    return list((await session.exec(statement)).all())
