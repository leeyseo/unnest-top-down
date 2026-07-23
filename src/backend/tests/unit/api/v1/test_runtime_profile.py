from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from langflow.api.v1.runtime import _contract_input, _contract_output, _immutable_agent_flow, _sandbox_payload
from langflow.api.v1.schemas import RunResponse, SimplifiedAPIRequest
from langflow.main import create_runtime_app
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow, FlowRead
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.user.model import User
from langflow.services.deps import get_settings_service
from lfx.graph.schema import ResultData, RunOutputs


def test_runtime_profile_mounts_only_deployment_routes(monkeypatch):
    settings = get_settings_service().settings
    monkeypatch.setattr(settings, "do_not_track", False)
    monkeypatch.setattr(settings, "deactivate_tracing", False)

    app = create_runtime_app()
    paths = set(app.openapi()["paths"])

    assert "/api/{api_version}/agent/run" in paths
    assert "/api/{api_version}/agent/stream" in paths
    assert "/api/{api_version}/sessions" in paths
    assert "/api/{api_version}/webhooks/{name}" in paths
    assert "/api/v1/files" in paths
    assert "/api/v1/files/{document_id}/download" in paths
    assert "/api/v1/ingestion/jobs/{job_id}" in paths
    assert "/api/v1/admin/api-keys" in paths
    assert "/health" in paths
    assert "/ready" in paths
    assert "/metrics" in paths
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in ("/flows", "/components", "/starter-projects", "/deployments/on-prem")
    )
    assert settings.do_not_track is True
    assert settings.deactivate_tracing is True


async def test_runtime_executes_release_snapshot_not_editable_draft(async_session, monkeypatch):
    monkeypatch.setenv("UNNEST_RUNTIME_SETUP_COMPLETE", "true")
    user = User(username="runtime-owner", password="unused", is_active=True)  # noqa: S106
    flow = Flow(
        name="runtime-agent",
        user_id=user.id,
        data={"nodes": [{"id": "draft"}], "edges": []},
    )
    version = FlowVersion(
        flow_id=flow.id,
        user_id=user.id,
        data={"nodes": [{"id": "released"}], "edges": []},
        version_number=1,
    )
    release = DeploymentRelease(
        user_id=user.id,
        version="1.0.0",
        agent_flow_version_id=version.id,
        ingestion_flow_version_id=version.id,
        config={},
        manifest={},
        api_version="v1",
    )
    async_session.add_all([user, flow, version, release])
    await async_session.flush()

    loaded_release, immutable_flow = await _immutable_agent_flow(async_session, "v1")

    assert loaded_release.id == release.id
    assert immutable_flow.data == {"nodes": [{"id": "released"}], "edges": []}


def test_runtime_applies_release_api_contract():
    release = DeploymentRelease(
        user_id=uuid4(),
        version="1.0.0",
        agent_flow_version_id=uuid4(),
        ingestion_flow_version_id=uuid4(),
        config={},
        manifest={
            "api": {
                "input_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                "input_mapping": {
                    "message": {"component_id": "agent-input", "component_field": "input_value"}
                },
                "output_mapping": {
                    "answer": {"component_id": "agent-output", "result_path": "message.text"}
                },
            }
        },
    )

    request = _contract_input(release, {"message": "hello"})
    assert request.tweaks is not None
    assert request.tweaks.root == {"agent-input": {"input_value": "hello"}}

    result = RunResponse(
        outputs=[
            RunOutputs(
                inputs={},
                outputs=[
                    ResultData(
                        component_id="agent-output",
                        results={"message": {"text": "world"}},
                    )
                ],
            )
        ]
    )
    assert _contract_output(release, result) == {"answer": "world"}

    with pytest.raises(HTTPException) as exc:
        _contract_input(release, {"message": 42})
    assert exc.value.status_code == 422


def test_sandbox_payload_preserves_whole_flow_boundary():
    user = User(username="sandbox-owner", password="unused", is_active=True)  # noqa: S106
    flow = FlowRead.model_validate(
        Flow(
            name="sandbox-agent",
            user_id=user.id,
            data={"nodes": [{"id": "custom"}], "edges": []},
        ),
        from_attributes=True,
    )
    release = DeploymentRelease(
        user_id=user.id,
        version="1.0.0",
        agent_flow_version_id=uuid4(),
        ingestion_flow_version_id=uuid4(),
        config={},
        manifest={
            "sandbox": {
                "required": True,
                "allowed_endpoints": ["https://models.internal/v1"],
            }
        },
    )

    payload = _sandbox_payload(
        release,
        flow,
        SimplifiedAPIRequest(tweaks={"custom": {"input_value": "hello"}}),
        user,
    )

    assert payload["execution_boundary"] == "whole-flow"
    assert payload["flow"]["data"] == flow.data
    assert payload["security"]["network"] == "deny-by-default"
    assert payload["security"]["allowed_endpoints"] == ["https://models.internal/v1"]


async def test_risky_release_dispatches_entire_flow_to_sandbox(monkeypatch):
    user = User(username="sandbox-dispatch", password="unused", is_active=True)  # noqa: S106
    flow = FlowRead.model_validate(
        Flow(name="sandbox-agent", user_id=user.id, data={"nodes": [], "edges": []}),
        from_attributes=True,
    )
    release = DeploymentRelease(
        user_id=user.id,
        version="1.0.0",
        agent_flow_version_id=uuid4(),
        ingestion_flow_version_id=uuid4(),
        config={},
        manifest={"sandbox": {"required": True}},
    )
    sandbox_run = AsyncMock(return_value="sandbox-stream")
    standard_run = AsyncMock()
    monkeypatch.setattr("langflow.api.v1.runtime._immutable_agent_flow", AsyncMock(return_value=(release, flow)))
    monkeypatch.setattr(
        "langflow.api.v1.runtime._contract_input",
        lambda *_args: SimplifiedAPIRequest(input_value="hello"),
    )
    monkeypatch.setattr("langflow.api.v1.runtime._run_in_sandbox", sandbox_run)
    monkeypatch.setattr("langflow.api.v1.runtime._run_flow_internal", standard_run)

    from langflow.api.v1.runtime import _run_agent

    result = await _run_agent(
        api_version="v1",
        stream=True,
        background_tasks=BackgroundTasks(),
        payload={"message": "hello"},
        current_user=user,
        session=None,  # type: ignore[arg-type]
        http_request=Request({"type": "http", "headers": []}),
    )

    assert result == "sandbox-stream"
    sandbox_run.assert_awaited_once()
    standard_run.assert_not_awaited()
