from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import uuid4

from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.runtime_bundle import load_bundled_runtime_release
from sqlmodel import select


async def test_runtime_imports_bundled_release_once(async_session, monkeypatch, tmp_path):
    agent_id, ingestion_id = uuid4(), uuid4()
    agent_data = {"nodes": [{"id": "agent"}], "edges": []}
    ingestion_data = {"nodes": [{"id": "ingestion"}], "edges": []}
    flows = [
        {
            "id": str(agent_id),
            "flow_id": str(uuid4()),
            "version_number": 1,
            "digest": canonical_digest(agent_data),
            "role": "agent",
        },
        {
            "id": str(ingestion_id),
            "flow_id": str(uuid4()),
            "version_number": 1,
            "digest": canonical_digest(ingestion_data),
            "role": "ingestion",
        },
    ]
    manifest = {
        "provider": "unnest-on-prem",
        "release_version": "8.7.6",
        "release_digest": canonical_digest(flows),
        "flows": flows,
        "api": {"version": "v8"},
        "deployment": {"default_language": "ko"},
        "acceptance_tests": [
            {
                "name": "health",
                "required": True,
                "request": {"path": "/health"},
                "expected": {"status": 200},
            }
        ],
        "knowledge_base_alias": "shared",
    }
    (tmp_path / "manifest").mkdir()
    (tmp_path / "flows").mkdir()
    (tmp_path / "manifest/release.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / f"flows/{agent_id}.json").write_text(json.dumps(agent_data), encoding="utf-8")
    (tmp_path / f"flows/{ingestion_id}.json").write_text(json.dumps(ingestion_data), encoding="utf-8")

    @asynccontextmanager
    async def test_session_scope():
        yield async_session

    monkeypatch.setenv("UNNEST_RELEASE_BUNDLE", str(tmp_path))
    monkeypatch.setattr("langflow.services.runtime_bundle.session_scope", test_session_scope)

    assert await load_bundled_runtime_release() is True
    await async_session.commit()
    assert await load_bundled_runtime_release() is False
    assert (await async_session.exec(select(DeploymentRelease))).one().api_version == "v8"
    assert len((await async_session.exec(select(FlowVersion))).all()) == 2
    assert (await async_session.exec(select(KnowledgeBaseRecord))).one().name == "shared"
