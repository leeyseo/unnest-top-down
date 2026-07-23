"""Conversation retention for the isolated runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete
from sqlmodel import col, select

from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.message.model import MessageTable

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


async def purge_expired_runtime_conversations(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    releases = (await session.exec(select(DeploymentRelease).order_by(col(DeploymentRelease.created_at).desc()))).all()
    deleted = 0
    seen_api_versions: set[str] = set()
    for release in releases:
        if release.api_version in seen_api_versions:
            continue
        seen_api_versions.add(release.api_version)
        deployment = release.manifest.get("deployment")
        config = deployment if isinstance(deployment, dict) else release.config
        retention_days = config.get("conversation_retention_days")
        if config.get("store_conversations") is not True or not isinstance(retention_days, int):
            continue
        version = await session.get(FlowVersion, release.agent_flow_version_id)
        if version is None:
            continue
        result = await session.exec(
            delete(MessageTable).where(
                MessageTable.flow_id == version.flow_id,
                col(MessageTable.timestamp) < current - timedelta(days=retention_days),
                MessageTable.session_metadata["api_version"].as_string() == release.api_version,  # type: ignore[index]
            )
        )
        deleted += max(getattr(result, "rowcount", 0) or 0, 0)
    return deleted
