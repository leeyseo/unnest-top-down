"""Load the immutable release bundled into an exported runtime image."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text
from sqlmodel import select

from langflow.services.database.models.deployment_release.model import (
    DeploymentAcceptanceTest,
    DeploymentRelease,
    DeploymentReleaseFlowVersion,
)
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.knowledge_base.model import KnowledgeBaseRecord
from langflow.services.database.models.user.model import User
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.deps import session_scope

_OWNER_ID = UUID("48b64f8d-04ca-5b61-bfd5-b0c7e5d20d22")
_OWNER_NAME = "__unnest_runtime__"
_IMPORT_LOCK_ID = 0x554E4E455354


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Bundled runtime release is missing or invalid: {path.name}"
        raise RuntimeError(msg) from exc
    if not isinstance(value, dict):
        msg = f"Bundled runtime release must contain a JSON object: {path.name}"
        raise TypeError(msg)
    return value


def _load_bundle(root: Path) -> tuple[dict, dict[str, dict]]:
    manifest = _read_json(root / "manifest" / "release.json")
    if manifest.get("provider") != "unnest-on-prem":
        msg = "Bundled runtime release has an unsupported provider"
        raise RuntimeError(msg)

    flows: dict[str, dict] = {}
    entries = manifest.get("flows")
    if not isinstance(entries, list) or not entries:
        msg = "Bundled runtime release does not declare flows"
        raise RuntimeError(msg)
    for entry in entries:
        version_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(version_id, str):
            msg = "Bundled runtime release contains an invalid Flow Version"
            raise TypeError(msg)
        data = _read_json(root / "flows" / f"{version_id}.json")
        if canonical_digest(data) != entry.get("digest"):
            msg = f"Bundled Flow Version digest mismatch: {version_id}"
            raise RuntimeError(msg)
        flows[version_id] = data
    return manifest, flows


async def load_bundled_runtime_release() -> bool:
    """Import the image bundle once after database migrations have completed."""
    configured = os.getenv("UNNEST_RELEASE_BUNDLE")
    if not configured:
        return False
    root = Path(configured).resolve()
    manifest, flows = _load_bundle(root)
    flow_entries = {str(entry["id"]): entry for entry in manifest["flows"]}
    roles = {
        role: [entry for entry in manifest["flows"] if entry.get("role") == role] for role in ("agent", "ingestion")
    }
    if any(len(entries) != 1 for entries in roles.values()):
        msg = "Bundled runtime release requires exactly one Agent and Ingestion Flow"
        raise RuntimeError(msg)

    release_id = uuid5(NAMESPACE_URL, str(manifest["release_digest"]))
    async with session_scope() as session:
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _IMPORT_LOCK_ID},
            )
        existing = (
            await session.exec(
                select(DeploymentRelease).where(DeploymentRelease.version == manifest["release_version"])
            )
        ).first()
        if existing is not None:
            if existing.manifest.get("release_digest") != manifest["release_digest"]:
                msg = f"Runtime release version already exists with different content: {manifest['release_version']}"
                raise RuntimeError(msg)
            return False

        owner = await session.get(User, _OWNER_ID)
        if owner is None:
            owner = User(
                id=_OWNER_ID,
                username=_OWNER_NAME,
                password=canonical_digest(str(uuid4())),
                is_active=False,
                is_superuser=False,
            )
            session.add(owner)
            await session.flush()

        for version_id, data in flows.items():
            entry = flow_entries[version_id]
            flow_id = UUID(str(entry["flow_id"]))
            flow = await session.get(Flow, flow_id)
            if flow is None:
                flow = Flow(
                    id=flow_id,
                    user_id=owner.id,
                    name=f"{entry['role']}-{str(flow_id)[:8]}",
                    data=data,
                    locked=True,
                )
                session.add(flow)
                await session.flush()
            version = await session.get(FlowVersion, UUID(version_id))
            if version is None:
                session.add(
                    FlowVersion(
                        id=UUID(version_id),
                        flow_id=flow_id,
                        user_id=owner.id,
                        version_number=int(entry["version_number"]),
                        data=data,
                        description=f"Bundled in release {manifest['release_version']}",
                    )
                )
            elif canonical_digest(version.data) != entry["digest"]:
                msg = f"Runtime Flow Version already exists with different content: {version_id}"
                raise RuntimeError(msg)
        await session.flush()

        agent = roles["agent"][0]
        ingestion = roles["ingestion"][0]
        subflows = [str(entry["id"]) for entry in manifest["flows"] if entry.get("role") == "subflow"]
        release = DeploymentRelease(
            id=release_id,
            user_id=owner.id,
            version=str(manifest["release_version"]),
            agent_flow_version_id=UUID(str(agent["id"])),
            ingestion_flow_version_id=UUID(str(ingestion["id"])),
            subflow_version_ids=subflows,
            config=dict(manifest["deployment"]),
            manifest=manifest,
            api_version=str(manifest["api"]["version"]),
        )
        session.add(release)
        await session.flush()
        session.add_all(
            [
                DeploymentReleaseFlowVersion(
                    release_id=release.id,
                    flow_version_id=UUID(str(entry["id"])),
                    role=str(entry["role"]),
                )
                for entry in manifest["flows"]
            ]
        )
        session.add_all(
            [
                DeploymentAcceptanceTest(
                    release_id=release.id,
                    name=str(test["name"]),
                    required=bool(test.get("required", True)),
                    request=dict(test["request"]),
                    expected=dict(test["expected"]),
                )
                for test in manifest["acceptance_tests"]
            ]
        )
        alias = manifest.get("knowledge_base_alias")
        if isinstance(alias, str) and alias:
            knowledge_base = (
                await session.exec(
                    select(KnowledgeBaseRecord).where(
                        KnowledgeBaseRecord.user_id == owner.id,
                        KnowledgeBaseRecord.name == alias,
                    )
                )
            ).first()
            if knowledge_base is None:
                session.add(KnowledgeBaseRecord(name=alias, user_id=owner.id))
    return True
