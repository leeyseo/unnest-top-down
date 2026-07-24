from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from fastapi import status
from langflow.services.deployment import WorkerArtifact, WorkerBuildStatus, next_api_version
from langflow.services.deployment.manifest import _acceptance_errors

if TYPE_CHECKING:
    from httpx import AsyncClient


def _node(node_id: str, node_type: str, **fields: Any) -> dict[str, Any]:
    template = {
        name: {"name": name, "value": value, "password": False, "load_from_db": False} for name, value in fields.items()
    }
    template["_type"] = node_type
    return {
        "id": node_id,
        "data": {
            "type": node_type,
            "node": {"name": node_type, "template": template},
        },
    }


def test_output_breaking_change_increments_api_version():
    previous = {
        "api": {
            "version": "v1",
            "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        }
    }
    changed_output = {"type": "object", "properties": {"answer": {"type": "object"}}}

    assert next_api_version(previous, previous["api"]["input_schema"], changed_output) == "v2"


def test_acceptance_contract_rejects_unsafe_paths_and_plaintext_credentials():
    errors = _acceptance_errors(
        [
            {
                "name": "unsafe",
                "required": True,
                "request": {
                    "path": "/health/../admin",
                    "body": {"api_key": "do-not-package"},
                },
                "expected": {"status": 200},
            }
        ]
    )

    assert any("safe absolute runtime path" in error for error in errors)
    assert "Acceptance tests must not contain plaintext credentials" in errors


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
        "config": {"base_image_digest": f"sha256:{'a' * 64}"},
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
            "input_mapping": {
                "message": {
                    "component_id": "agent-input",
                    "component_field": "input_value",
                }
            },
            "output_mapping": {
                "answer": {
                    "component_id": "agent-output",
                    "result_path": "message.text",
                }
            },
        },
    }


async def _root_versions(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    agent_version_id = await _snapshot(
        client,
        headers,
        "agent-release-flow",
        {
            "nodes": [
                _node("agent-input", "ChatInput", input_value=""),
                _node("agent-output", "ChatOutput"),
                _node("retrieval", "KnowledgeBase", knowledge_base="shared"),
            ],
            "edges": [],
        },
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


async def test_validation_rejects_direct_message_store_when_conversation_storage_is_off(
    client: AsyncClient,
    logged_in_headers_super_user,
):
    agent_version_id = await _snapshot(
        client,
        logged_in_headers_super_user,
        "agent-with-direct-store",
        {
            "nodes": [
                _node("agent-input", "ChatInput", input_value=""),
                _node("direct-store", "StoreMessage"),
                _node("agent-output", "ChatOutput"),
                _node("retrieval", "KnowledgeBase", knowledge_base="shared"),
            ],
            "edges": [],
        },
    )
    ingestion_version_id = await _snapshot(
        client,
        logged_in_headers_super_user,
        "ingestion-for-direct-store",
        {
            "nodes": [
                _node("file", "DeploymentFileInput"),
                _node("knowledge", "Knowledge", knowledge_base="shared"),
            ],
            "edges": [{"source": "file", "target": "knowledge"}],
        },
    )
    payload = _release_payload(agent_version_id, ingestion_version_id)

    response = await client.post(
        "/api/v1/deployments/on-prem/releases/validate",
        headers=logged_in_headers_super_user,
        json=payload,
    )

    assert response.status_code == status.HTTP_200_OK
    assert any("direct conversation storage components (direct-store)" in error for error in response.json()["errors"])


async def test_create_on_prem_release_from_saved_versions(
    client: AsyncClient, logged_in_headers_super_user, monkeypatch
):
    logged_in_headers = logged_in_headers_super_user
    agent_version_id, ingestion_version_id = await _root_versions(client, logged_in_headers)
    source_contents = b"source document selected by the SI release author"
    uploaded = await client.post(
        "/api/v2/files/",
        headers=logged_in_headers,
        files={"file": ("guide.txt", source_contents, "text/plain")},
    )
    assert uploaded.status_code == status.HTTP_201_CREATED
    source_file_id = uploaded.json()["id"]
    payload = _release_payload(agent_version_id, ingestion_version_id)
    payload["source_file_ids"] = [source_file_id]

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
    assert body["source_file_ids"] == [source_file_id]
    assert body["manifest"]["source_documents"] == [
        {
            "id": source_file_id,
            "name": "guide.txt",
            "size_bytes": len(source_contents),
            "digest": f"sha256:{hashlib.sha256(source_contents).hexdigest()}",
            "mime_type": "text/plain",
            "package_path": f"documents/source/{source_file_id}/guide.txt",
        }
    ]
    assert set(body["manifest"]["services"]) == {
        "runtime",
        "redis",
        "postgresql",
        "sandbox-controller",
        "sandbox-executor",
        "sandbox-gateway",
        "sandbox-egress-proxy",
    }
    assert {
        "service": "runtime",
        "name": "https",
        "port": 7860,
        "protocol": "tcp",
        "scope": "host",
    } in body["manifest"]["ports"]
    assert body["manifest"]["api"]["input_mapping"]["message"]["component_id"] == "agent-input"
    assert body["manifest"]["acceptance_tests"][0] == {
        "name": "health",
        "required": True,
        "request": {"path": "/health"},
        "expected": {"status": 200},
    }
    assert "tests/acceptance.json" in body["manifest"]["package"]["required_files"]
    assert body["manifest"]["build"]["sbom_required"] is True
    assert body["manifest"]["build"]["signer_fingerprint"] is None
    assert body["manifest"]["package"]["layout_version"] == 2
    builds = await client.get(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds",
        headers=logged_in_headers,
    )
    assert builds.status_code == status.HTTP_200_OK
    build = builds.json()["builds"][0]
    assert build["status"] == "pending"

    delete = await client.delete(
        f"/api/v1/flows/{body['manifest']['flows'][0]['flow_id']}/versions/{agent_version_id}",
        headers=logged_in_headers,
    )
    assert delete.status_code == status.HTTP_409_CONFLICT

    class FakeWorker:
        payload = None
        sync_count = 0
        artifact_content = b"offline-package"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def submit(self, submitted, source_documents=None):
            self.payload = submitted
            assert source_documents is not None
            assert [(document_id, path.read_bytes()) for document_id, path in source_documents] == [
                (source_file_id, source_contents)
            ]
            return WorkerBuildStatus(job_id=submitted["build_id"], status="queued")

        async def get(self, _job_id):
            self.sync_count += 1
            if self.sync_count == 1:
                return WorkerBuildStatus(
                    job_id=self.payload["build_id"],
                    status="blocked",
                    scan_report={"critical": 1},
                )
            return WorkerBuildStatus(
                job_id=self.payload["build_id"],
                status="succeeded",
                scan_report={"critical": 0},
                artifacts=[
                    WorkerArtifact(
                        artifact_type="package",
                        location="release.tar",
                        digest=f"sha256:{hashlib.sha256(self.artifact_content).hexdigest()}",
                        size_bytes=len(self.artifact_content),
                        checksums={"release.tar": f"sha256:{hashlib.sha256(self.artifact_content).hexdigest()}"},
                        sbom={"bomFormat": "CycloneDX"},
                        signature="cosign-signature",
                    )
                ],
            )

        async def download_artifact(self, _job_id, _artifact_type, destination, *, max_bytes):
            assert max_bytes == len(self.artifact_content)
            destination.write_bytes(self.artifact_content)
            return (
                f"sha256:{hashlib.sha256(self.artifact_content).hexdigest()}",
                len(self.artifact_content),
            )

        async def push_registry(self, _job_id, *, reference, credential_secret_name):
            assert credential_secret_name == "REGISTRY_CREDENTIAL"  # noqa: S105
            return WorkerArtifact(
                artifact_type="registry",
                location=reference,
                digest=f"sha256:{hashlib.sha256(self.artifact_content).hexdigest()}",
                checksums={"image": f"sha256:{hashlib.sha256(self.artifact_content).hexdigest()}"},
                sbom={"bomFormat": "CycloneDX"},
                signature="cosign-signature",
            )

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
    assert worker.payload["manifest"]["acceptance_tests"] == body["manifest"]["acceptance_tests"]

    synced = await client.post(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds/{build['id']}/sync",
        headers=logged_in_headers,
    )
    assert synced.status_code == status.HTTP_200_OK
    assert synced.json()["status"] == "blocked"

    overridden = await client.patch(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds/{build['id']}/critical-override",
        headers=logged_in_headers,
        json={"reason": "Accepted for the isolated test environment"},
    )
    assert overridden.status_code == status.HTTP_200_OK
    assert overridden.json()["status"] == "pending"

    submitted = await client.post(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds/{build['id']}/submit",
        headers=logged_in_headers,
    )
    assert submitted.status_code == status.HTTP_200_OK
    succeeded = await client.post(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds/{build['id']}/sync",
        headers=logged_in_headers,
    )
    assert succeeded.status_code == status.HTTP_200_OK
    assert succeeded.json()["status"] == "succeeded"
    assert succeeded.json()["artifacts"][0]["expires_at"] is not None
    artifact = succeeded.json()["artifacts"][0]

    downloaded = await client.get(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds/{build['id']}/artifacts/{artifact['id']}/download",
        headers=logged_in_headers,
    )
    assert downloaded.status_code == status.HTTP_200_OK
    assert downloaded.content == worker.artifact_content
    assert downloaded.headers["x-checksum-sha256"] == hashlib.sha256(worker.artifact_content).hexdigest()

    pushed = await client.post(
        f"/api/v1/deployments/on-prem/releases/{body['id']}/builds/{build['id']}/registry",
        headers=logged_in_headers,
        json={
            "reference": "registry.internal/agency/unnest:1.0.0",
            "credential_secret_name": "REGISTRY_CREDENTIAL",
        },
    )
    assert pushed.status_code == status.HTTP_201_CREATED
    assert pushed.json()["location"] == "registry.internal/agency/unnest:1.0.0"

    audit = await client.get(
        "/api/v1/authz/audit",
        headers=logged_in_headers,
        params={"action": "deployment:critical_override"},
    )
    assert audit.status_code == status.HTTP_200_OK
    assert audit.json()["items"][0]["details"]["reason"] == "Accepted for the isolated test environment"

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
        {
            "nodes": [
                secret_node,
                _node("agent-input", "ChatInput", input_value=""),
                _node("agent-output", "ChatOutput"),
                _node("retrieval-secret", "KnowledgeBase", knowledge_base="shared"),
            ],
            "edges": [],
        },
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


async def test_validate_on_prem_release_requires_api_component_mappings(
    client: AsyncClient,
    logged_in_headers,
):
    agent_version_id, ingestion_version_id = await _root_versions(client, logged_in_headers)
    payload = _release_payload(agent_version_id, ingestion_version_id)
    payload["api"]["input_mapping"] = {}

    response = await client.post(
        "/api/v1/deployments/on-prem/releases/validate",
        headers=logged_in_headers,
        json=payload,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["manifest"] is None
    assert "Required API input fields are not mapped: message" in response.json()["errors"]


async def test_validate_on_prem_release_routes_risky_flow_to_declared_sandbox_network(
    client: AsyncClient,
    logged_in_headers,
):
    risky = _node("repl", "PythonREPL")
    risky["data"]["node"]["metadata"] = {
        "deployment": {
            "sandbox": True,
            "internal_endpoints": ["https://models.internal/v1"],
            "python_packages": [
                {
                    "name": "agency-sdk",
                    "version": "1.2.3",
                    "hashes": [f"sha256:{'a' * 64}"],
                }
            ],
        }
    }
    agent_version_id = await _snapshot(
        client,
        logged_in_headers,
        "sandbox-agent",
        {
            "nodes": [
                _node("agent-input", "ChatInput", input_value=""),
                _node("agent-output", "ChatOutput"),
                _node("retrieval", "KnowledgeBase", knowledge_base="shared"),
                risky,
            ],
            "edges": [],
        },
    )
    _unused_agent, ingestion_version_id = await _root_versions(client, logged_in_headers)

    response = await client.post(
        "/api/v1/deployments/on-prem/releases/validate",
        headers=logged_in_headers,
        json=_release_payload(agent_version_id, ingestion_version_id),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["errors"] == []
    assert response.json()["manifest"]["sandbox"] == {
        "required": True,
        "risky_flow_version_ids": [str(agent_version_id)],
        "max_attachment_bytes": 512 * 1024 * 1024,
        "network_policy": "deny-by-default",
        "allowed_endpoints": ["https://models.internal/v1"],
    }


async def test_validate_on_prem_release_rejects_unlocked_custom_dependency(
    client: AsyncClient,
    logged_in_headers,
):
    risky = _node("custom", "CustomComponent")
    risky["data"]["node"]["metadata"] = {
        "deployment": {
            "python_packages": [
                {
                    "name": "agency-sdk",
                    "version": ">=1.2.3",
                    "hashes": [],
                }
            ]
        }
    }
    agent_version_id = await _snapshot(
        client,
        logged_in_headers,
        "unlocked-custom-agent",
        {
            "nodes": [
                _node("agent-input", "ChatInput", input_value=""),
                _node("agent-output", "ChatOutput"),
                _node("retrieval", "KnowledgeBase", knowledge_base="shared"),
                risky,
            ],
            "edges": [],
        },
    )
    _unused_agent, ingestion_version_id = await _root_versions(client, logged_in_headers)

    response = await client.post(
        "/api/v1/deployments/on-prem/releases/validate",
        headers=logged_in_headers,
        json=_release_payload(agent_version_id, ingestion_version_id),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["manifest"] is None
    assert any(".version must be an exact version" in error for error in response.json()["errors"])
    assert any(".hashes must contain" in error for error in response.json()["errors"])
