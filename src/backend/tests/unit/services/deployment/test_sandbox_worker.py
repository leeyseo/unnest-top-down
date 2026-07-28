from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import anyio
import httpx
import pytest
from fastapi import FastAPI, Request
from langflow.api.v1.schemas import RunResponse
from langflow.services.database.models.flow.model import Flow, FlowRead
from langflow.services.database.models.user.model import User
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.deployment.sandbox import SANDBOX_ATTACHMENT_PATH, SANDBOX_FRAME_HEADER
from langflow.services.deployment.sandbox_worker import (
    SandboxExecutionRequest,
    SandboxValidationError,
    create_sandbox_controller_app,
    create_sandbox_worker_app,
    validate_sandbox_execution,
)


def _bundle(root):
    user = User(username="sandbox-user", password="unused", is_active=True)  # noqa: S106
    agent = FlowRead.model_validate(
        Flow(
            name="sandbox-agent",
            user_id=user.id,
            data={"nodes": [{"id": "risky"}], "edges": []},
        ),
        from_attributes=True,
    )
    ingestion_id = uuid4()
    ingestion_flow_id = uuid4()
    ingestion_data = {
        "nodes": [
            {
                "id": "file-input",
                "data": {
                    "type": "DeploymentFileInput",
                    "node": {"name": "DeploymentFileInput"},
                },
            }
        ],
        "edges": [],
    }
    release_digest = canonical_digest({"agent": agent.data, "ingestion": ingestion_data})
    manifest = {
        "provider": "unnest-on-prem",
        "release_version": "1.0.0",
        "release_digest": release_digest,
        "flows": [
            {
                "id": str(uuid4()),
                "flow_id": str(agent.id),
                "version_number": 1,
                "digest": canonical_digest(agent.data),
                "role": "agent",
            },
            {
                "id": str(ingestion_id),
                "flow_id": str(ingestion_flow_id),
                "version_number": 1,
                "digest": canonical_digest(ingestion_data),
                "role": "ingestion",
            },
        ],
        "sandbox": {
            "required": True,
            "allowed_endpoints": ["https://models.internal/v1"],
            "max_attachment_bytes": 512 * 1024 * 1024,
        },
        "secret_names": ["MODEL_TOKEN"],
        "source_documents": [],
    }
    (root / "manifest").mkdir()
    (root / "flows").mkdir()
    (root / "manifest/release.json").write_text(json.dumps(manifest), encoding="utf-8")
    for entry, data in zip(manifest["flows"], (agent.data, ingestion_data), strict=True):
        (root / f"flows/{entry['id']}.json").write_text(json.dumps(data), encoding="utf-8")
    return {
        "execution_boundary": "whole-flow",
        "flow_role": "agent",
        "release_id": str(uuid5(NAMESPACE_URL, release_digest)),
        "flow_version_id": manifest["flows"][0]["id"],
        "user_id": str(user.id),
        "flow": agent.model_dump(mode="json"),
        "context": {
            "deployment_release_id": str(uuid5(NAMESPACE_URL, release_digest)),
            "deployment_subflows": {},
            "runtime_session_metadata": {
                "user_id": str(user.id),
                "deployment_release_id": str(uuid5(NAMESPACE_URL, release_digest)),
                "api_version": "v1",
                "trigger": "api",
            },
        },
        "request": {"input_value": "hello"},
        "security": {
            "run_as_non_root": True,
            "read_only_root_filesystem": True,
            "drop_capabilities": ["ALL"],
            "network": "deny-by-default",
            "allowed_endpoints": ["https://models.internal/v1"],
        },
        "secrets": {"MODEL_TOKEN": "government-secret"},
    }


def test_sandbox_request_is_bound_to_signed_flow_policy_and_secret_names(tmp_path):
    payload = SandboxExecutionRequest.model_validate(_bundle(tmp_path))

    execution = validate_sandbox_execution(payload, bundle_root=tmp_path)

    assert execution.request.input_value == "hello"
    assert execution.secrets["MODEL_TOKEN"].get_secret_value() == "government-secret"

    changed = payload.model_copy(deep=True)
    changed.flow["data"]["nodes"].append({"id": "injected"})
    with pytest.raises(SandboxValidationError, match="Flow does not match"):
        validate_sandbox_execution(changed, bundle_root=tmp_path)

    unexpected_secret = payload.model_copy(
        update={"secrets": {**payload.secrets, "UNDECLARED": "exfiltrate"}}
    )
    with pytest.raises(SandboxValidationError, match="secret names"):
        validate_sandbox_execution(unexpected_secret, bundle_root=tmp_path)


async def test_sandbox_worker_rejects_tampered_flow_before_executor(tmp_path, monkeypatch):
    payload = _bundle(tmp_path)
    payload["flow"]["data"]["nodes"].append({"id": "injected"})
    called = False

    async def executor(_execution, _stream, _background_tasks, _request):
        nonlocal called
        called = True
        return RunResponse(outputs=[], session_id="sandbox")

    monkeypatch.setenv("UNNEST_SANDBOX_RELEASE_BUNDLE", str(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_sandbox_worker_app(executor)),
        base_url="https://sandbox.internal",
    ) as client:
        response = await client.post("/v1/flows/run", json=payload)

    assert response.status_code == 422
    assert called is False


async def test_sandbox_worker_enforces_whole_execution_timeout(tmp_path, monkeypatch):
    payload = _bundle(tmp_path)

    async def executor(_execution, _stream, _background_tasks, _request):
        await anyio.sleep(1)
        return RunResponse(outputs=[], session_id="sandbox")

    monkeypatch.setenv("UNNEST_SANDBOX_RELEASE_BUNDLE", str(tmp_path))
    monkeypatch.setattr(
        "langflow.services.deployment.sandbox_worker._execution_timeout_seconds",
        lambda: 0.001,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_sandbox_worker_app(executor)),
        base_url="https://sandbox.internal",
    ) as client:
        response = await client.post("/v1/flows/run", json=payload)

    assert response.status_code == 504
    assert response.json()["detail"] == "Sandbox execution timed out"


async def test_controller_forwards_unmasked_secrets_only_after_release_validation(
    tmp_path,
    monkeypatch,
):
    payload = _bundle(tmp_path)
    received = {}
    executor = FastAPI()

    @executor.post("/v1/flows/run")
    async def run(request: Request):
        received.update(await request.json())
        return RunResponse(outputs=[], session_id="sandbox")

    monkeypatch.setenv("UNNEST_SANDBOX_RELEASE_BUNDLE", str(tmp_path))
    controller = create_sandbox_controller_app(
        executor_url="http://sandbox-executor:8091",
        transport=httpx.ASGITransport(app=executor),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller),
        base_url="https://sandbox-controller.internal",
    ) as client:
        response = await client.post("/v1/flows/run", json=payload)

    assert response.status_code == 200
    assert received["secrets"] == {"MODEL_TOKEN": "government-secret"}


async def test_ingestion_attachment_is_verified_and_exposed_only_as_a_temporary_file(
    tmp_path,
    monkeypatch,
):
    payload = _bundle(tmp_path)
    manifest = json.loads((tmp_path / "manifest/release.json").read_text())
    ingestion = manifest["flows"][1]
    ingestion_data = json.loads((tmp_path / f"flows/{ingestion['id']}.json").read_text())
    contents = b"classified government document"
    payload.update(
        {
            "flow_role": "ingestion",
            "flow_version_id": ingestion["id"],
            "flow": {
                **payload["flow"],
                "id": ingestion["flow_id"],
                "name": "sandbox-ingestion",
                "data": ingestion_data,
            },
            "request": {
                "output_type": "any",
                "tweaks": {
                    "file-input": {
                        "file_path": SANDBOX_ATTACHMENT_PATH,
                        "document_id": str(uuid4()),
                    }
                },
            },
            "attachment": {
                "component_id": "file-input",
                "checksum": f"sha256:{hashlib.sha256(contents).hexdigest()}",
                "size_bytes": len(contents),
            },
        }
    )
    observed_path = None

    async def executor(execution, stream, _background_tasks, _request):
        nonlocal observed_path
        assert stream is False
        observed_path = execution.request.tweaks.root["file-input"]["file_path"]
        assert Path(observed_path).read_bytes() == contents
        return RunResponse(outputs=[], session_id="sandbox")

    monkeypatch.setenv("UNNEST_SANDBOX_RELEASE_BUNDLE", str(tmp_path))
    worker = create_sandbox_worker_app(executor)
    controller = create_sandbox_controller_app(
        executor_url="http://sandbox-executor:8091",
        transport=httpx.ASGITransport(app=worker),
    )
    metadata = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = SANDBOX_FRAME_HEADER.pack(len(metadata)) + metadata + contents
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller),
        base_url="https://sandbox-controller.internal",
    ) as client:
        response = await client.post(
            "/v1/flows/ingestion",
            content=body,
            headers={"content-type": "application/vnd.unnest.sandbox-ingestion"},
        )

    assert response.status_code == 200, response.text
    assert observed_path is not None
    assert not Path(observed_path).exists()

    tampered = body[:-1] + b"x"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker),
        base_url="http://sandbox-executor.internal",
    ) as client:
        rejected = await client.post(
            "/v1/flows/ingestion",
            content=tampered,
            headers={"content-type": "application/vnd.unnest.sandbox-ingestion"},
        )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "Sandbox ingestion attachment does not match"
