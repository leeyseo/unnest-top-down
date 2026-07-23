from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import status
from langflow.services.deployment import WorkerBuildStatus

if TYPE_CHECKING:
    from httpx import AsyncClient


def _node(node_id: str, node_type: str, **fields: Any) -> dict[str, Any]:
    template = {
        name: {"name": name, "value": value, "password": False, "load_from_db": False}
        for name, value in fields.items()
    }
    template["_type"] = node_type
    return {
        "id": node_id,
        "data": {
            "type": node_type,
            "node": {"name": node_type, "template": template},
        },
    }


async def _snapshot(client: AsyncClient, headers: dict[str, str], name: str, data: dict[str, Any]) -> str:
    response = await client.post(
        "/api/v1/flows/",
        headers=headers,
        json={"name": name, "description": name, "data": data, "is_component": False},
    )
    assert response.status_code == status.HTTP_201_CREATED
    flow_id = response.json()["id"]
    response = await client.post(f"/api/v1/flows/{flow_id}/versions/", headers=headers, json={})
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()["id"]


def _release_payload(agent_version_id: str, ingestion_version_id: str) -> dict[str, Any]:
    return {
        "release_version": "1.0.0",
        "agent_flow_version_id": agent_version_id,
        "ingestion_flow_version_id": ingestion_version_id,
        "api": {
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            "output_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
            "request_example": {"message": "hello"},
            "response_example": {"answer": "hello"},
        },
    }


async def _root_versions(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    agent_version_id = await _snapshot(
        client,
        headers,
        "agent-release-flow",
        {"nodes": [_node("retrieval", "KnowledgeBase", knowledge_base="shared")], "edges": []},
    )
    ingestion_version_id = await _snapshot(
        client,
        headers,
        "ingestion-release-flow",
        {
            "nodes": [
                _node("file", "DeploymentFileInput"),
                _node("knowledge", "Knowledge", knowledge_base="shared"),
            ],
            "edges": [{"source": "file", "target": "knowledge"}],
        },
    )
    return agent_version_id, ingestion_version_id


async def test_create_on_prem_release_from_saved_versions(client: AsyncClient, logged_in_headers, monkeypatch):
    agent_version_id, ingestion_version_id = await _root_versions(client, logged_in_headers)
    payload = _release_payload(agent_version_id, ingestion_version_id)

    response = await client.post(
        "/api/v1/deployments/on-prem/releases",
        headers=logged_in_headers,
        json=payload,
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["release_version"] == "1.0.0"
    assert body["api_version"] == "v1"
    assert body["manifest"]["provider"] == "unnest-on-prem"
    assert [flow["role"] for flow in body["manifest"]["flows"]] == ["agent", "ingestion"]
    assert body["manifest"]["secret_names"] == []
    assert body["manifest"]["build"]["sbom_required"] is True
    builds = await client.get(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds",
        headers=logged_in_headers,
    )
    assert builds.status_code == status.HTTP_200_OK
    build = builds.json()["builds"][0]
    assert build["status"] == "pending"

    class FakeWorker:
        payload = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def submit(self, submitted):
            self.payload = submitted
            return WorkerBuildStatus(job_id="worker-job-1", status="queued")

    worker = FakeWorker()
    monkeypatch.setattr(
        "langflow.api.v1.on_prem_deployments._worker_client_or_503",
        lambda: worker,
    )
    submitted = await client.post(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds/{build['id']}/submit",
        headers=logged_in_headers,
    )
    assert submitted.status_code == status.HTTP_200_OK
    assert submitted.json()["status"] == "queued"
    assert worker.payload["reproducible"] == {"source_date_epoch": 0, "sort_files": True}

    duplicate = await client.post(
        "/api/v1/deployments/on-prem/releases",
        headers=logged_in_headers,
        json=payload,
    )
    assert duplicate.status_code == status.HTTP_409_CONFLICT


async def test_validate_on_prem_release_rejects_plaintext_flow_secret(client: AsyncClient, logged_in_headers):
    agent_version_id, ingestion_version_id = await _root_versions(client, logged_in_headers)
    secret_node = _node("model", "LanguageModel")
    secret_node["data"]["node"]["template"]["api_key"] = {
        "name": "api_key",
        "value": "do-not-package",  # pragma: allowlist secret
        "password": True,
        "load_from_db": False,
    }
    agent_version_id = await _snapshot(
        client,
        logged_in_headers,
        "agent-with-secret",
        {"nodes": [secret_node, _node("retrieval-secret", "KnowledgeBase", knowledge_base="shared")], "edges": []},
    )

    response = await client.post(
        "/api/v1/deployments/on-prem/releases/validate",
        headers=logged_in_headers,
        json=_release_payload(agent_version_id, ingestion_version_id),
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["manifest"] is None
    assert any("plaintext value" in error for error in body["errors"])
    assert "do-not-package" not in response.text
