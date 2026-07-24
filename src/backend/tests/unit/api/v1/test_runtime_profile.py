from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4, uuid5

import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from langflow.api.v1.runtime import (
    _apply_active_index_tweaks,
    _apply_conversation_policy,
    _bundled_source_documents_ready,
    _contract_input,
    _contract_output,
    _immutable_agent_flow,
    _immutable_subflows,
    _sandbox_payload,
    execute_scheduled_agent,
    list_sessions,
)
from langflow.api.v1.schemas import RunResponse, SimplifiedAPIRequest
from langflow.main import create_runtime_app
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow, FlowRead
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.message.model import MessageTable
from langflow.services.database.models.runtime_document import (
    DocumentVersion,
    IndexGeneration,
    RuntimeDocument,
)
from langflow.services.database.models.runtime_schedule import RuntimeSchedule
from langflow.services.database.models.user.model import User
from langflow.services.deps import get_settings_service
from lfx.custom.custom_component.component import Component
from lfx.graph.schema import ResultData, RunOutputs
from lfx.schema.message import Message


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
    assert "/api/v1/ingestion/jobs/{job_id}/retry" in paths
    assert "/api/v1/admin/api-keys" in paths
    assert "/api/v1/admin/users" in paths
    assert "/api/v1/admin/users/{user_id}" in paths
    assert "/api/v1/admin/backups" in paths
    assert "/api/v1/admin/backups/{backup_id}/download" in paths
    assert "/api/v1/admin/backups/{backup_id}/verify" in paths
    assert "/api/v1/admin/audit" in paths
    assert "/api/v1/admin/audit/checkpoints" in paths
    assert "/api/v1/admin/license" in paths
    assert "/api/v1/admin/schedules" in paths
    assert "/api/v1/admin/schedules/{schedule_id}" in paths
    assert "/api/v1/setup" in paths
    assert "/api/v1/setup/status" in paths
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


async def test_bundled_documents_block_runtime_until_indexing_is_active(async_session):
    source_id = uuid4()
    user = User(username="bundled-document-owner", password="unused", is_active=True)  # noqa: S106
    knowledge_base = KnowledgeBaseRecord(name="shared", user_id=user.id)
    release = DeploymentRelease(
        user_id=user.id,
        version="1.0.0",
        agent_flow_version_id=uuid4(),
        ingestion_flow_version_id=uuid4(),
        config={},
        manifest={"source_documents": [{"id": str(source_id)}]},
        api_version="v1",
    )
    document = RuntimeDocument(
        id=uuid5(release.id, str(source_id)),
        user_id=user.id,
        knowledge_base_id=knowledge_base.id,
        name="guide.txt",
        status="pending",
    )
    async_session.add_all([user, knowledge_base, document])
    await async_session.flush()

    assert await _bundled_source_documents_ready(async_session, release) is False

    document.status = "active"
    async_session.add(document)
    await async_session.flush()

    assert await _bundled_source_documents_ready(async_session, release) is True


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


async def test_runtime_bundles_release_pinned_subflow_snapshot(async_session):
    user = User(username="runtime-subflow-owner", password="unused", is_active=True)  # noqa: S106
    agent = Flow(name="runtime-agent-root", user_id=user.id, data={"nodes": [], "edges": []})
    subflow = Flow(
        name="runtime-subflow",
        user_id=user.id,
        data={"nodes": [{"id": "draft"}], "edges": []},
    )
    agent_version = FlowVersion(
        flow_id=agent.id,
        user_id=user.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    subflow_version = FlowVersion(
        flow_id=subflow.id,
        user_id=user.id,
        data={"nodes": [{"id": "released"}], "edges": []},
        version_number=1,
    )
    release = DeploymentRelease(
        user_id=user.id,
        version="1.0.0",
        agent_flow_version_id=agent_version.id,
        ingestion_flow_version_id=agent_version.id,
        subflow_version_ids=[str(subflow_version.id)],
        config={},
        manifest={},
        api_version="v1",
    )
    async_session.add_all([user, agent, subflow, agent_version, subflow_version, release])
    await async_session.flush()

    bundled = await _immutable_subflows(async_session, release)

    assert bundled[str(subflow.id)]["flow_version_id"] == str(subflow_version.id)
    assert bundled[str(subflow.id)]["data"] == {"nodes": [{"id": "released"}], "edges": []}


async def test_runtime_retrieval_uses_active_physical_index(async_session):
    user = User(username="runtime-index-owner", password="unused", is_active=True)  # noqa: S106
    kb = KnowledgeBaseRecord(name="shared", user_id=user.id)
    flow = Flow(
        name="runtime-index-agent",
        user_id=user.id,
        data={
            "nodes": [
                {
                    "id": "knowledge",
                    "data": {"node": {"template": {"knowledge_base": {"value": "shared"}}}},
                }
            ],
            "edges": [],
        },
    )
    release = DeploymentRelease(
        user_id=user.id,
        version="1.0.0",
        agent_flow_version_id=uuid4(),
        ingestion_flow_version_id=uuid4(),
        config={},
        manifest={"knowledge_base_alias": "shared"},
        api_version="v1",
    )
    generation = IndexGeneration(
        knowledge_base_id=kb.id,
        fingerprint="sha256:index",
        status="active",
        is_active=True,
        backend_reference={"alias": "shared--physical"},
    )
    document = RuntimeDocument(
        user_id=user.id,
        knowledge_base_id=kb.id,
        name="policy.txt",
        status="active",
    )
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        checksum=f"sha256:{'e' * 64}",
        mime_type="text/plain",
        size_bytes=6,
        storage_path="runtime-documents/policy.txt",
        status="active",
    )
    async_session.add_all([user, kb, flow, generation, document, version])
    await async_session.flush()
    request = SimplifiedAPIRequest(output_type="any", tweaks={})

    await _apply_active_index_tweaks(
        async_session,
        release=release,
        flow=FlowRead.model_validate(flow, from_attributes=True),
        input_request=request,
    )

    assert request.tweaks is not None
    assert request.tweaks.root["knowledge"] == {
        "knowledge_base": "shared--physical",
        "metadata_filter": f'{{"runtime_document_version_id": ["{version.id}"]}}',
    }


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
                "input_mapping": {"message": {"component_id": "agent-input", "component_field": "input_value"}},
                "output_mapping": {"answer": {"component_id": "agent-output", "result_path": "message.text"}},
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


def test_runtime_disables_component_message_storage_when_release_policy_is_off():
    release = DeploymentRelease(
        user_id=uuid4(),
        version="1.0.0",
        agent_flow_version_id=uuid4(),
        ingestion_flow_version_id=uuid4(),
        config={"store_conversations": False},
        manifest={"deployment": {"store_conversations": False}},
    )
    flow = FlowRead.model_validate(
        Flow(
            name="conversation-policy",
            user_id=release.user_id,
            data={
                "nodes": [
                    {
                        "id": "input",
                        "data": {
                            "node": {
                                "template": {
                                    "should_store_message": {"value": True},
                                }
                            }
                        },
                    },
                    {
                        "id": "output",
                        "data": {
                            "node": {
                                "template": {
                                    "should_store_message": {"value": True},
                                }
                            }
                        },
                    },
                ],
                "edges": [],
            },
        ),
        from_attributes=True,
    )
    request = SimplifiedAPIRequest(tweaks={"input": {"input_value": "hello"}})

    _apply_conversation_policy(release, flow, request)

    assert request.tweaks is not None
    assert request.tweaks.root["input"]["should_store_message"] is False
    assert request.tweaks.root["output"]["should_store_message"] is False


async def test_runtime_session_metadata_is_added_to_stored_messages(monkeypatch):
    class RuntimeMetadataComponent(Component):
        def build(self) -> None:
            pass

    graph = MagicMock()
    graph.flow_id = str(uuid4())
    graph.run_id = str(uuid4())
    graph.context = {
        "runtime_session_metadata": {
            "user_id": "trusted-user",
            "api_version": "v1",
        }
    }
    component = RuntimeMetadataComponent(_vertex=MagicMock(graph=graph), _tracing_service=None)
    message = Message(
        text="hello",
        sender="User",
        sender_name="User",
        session_id="session-1",
        session_metadata={"user_id": "untrusted-user", "client": "kept"},
    )
    store = AsyncMock(return_value=[message])
    monkeypatch.setattr("lfx.custom.custom_component.component.astore_message", store)

    await component._store_message(message)

    stored_message = store.await_args.args[0]
    assert stored_message.session_metadata == {
        "user_id": "trusted-user",
        "client": "kept",
        "api_version": "v1",
    }


async def test_runtime_sessions_are_owner_scoped_and_hidden_when_storage_is_off(async_session, monkeypatch):
    monkeypatch.setenv("UNNEST_RUNTIME_SETUP_COMPLETE", "true")
    release_owner = User(username="release-owner", password="unused", is_active=True)  # noqa: S106
    runtime_user = User(username="runtime-user", password="unused", is_active=True)  # noqa: S106
    other_user = User(username="other-user", password="unused", is_active=True)  # noqa: S106
    flow = Flow(name="session-agent", user_id=release_owner.id, data={"nodes": [], "edges": []})
    version = FlowVersion(
        flow_id=flow.id,
        user_id=release_owner.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    release = DeploymentRelease(
        user_id=release_owner.id,
        version="1.0.0",
        agent_flow_version_id=version.id,
        ingestion_flow_version_id=version.id,
        api_version="v1",
        config={"store_conversations": True},
        manifest={"deployment": {"store_conversations": True}},
    )
    own_message = MessageTable(
        sender="User",
        sender_name="User",
        text="mine",
        session_id="own-session",
        flow_id=flow.id,
        session_metadata={"user_id": str(runtime_user.id), "api_version": "v1"},
    )
    other_message = MessageTable(
        sender="User",
        sender_name="User",
        text="other",
        session_id="other-session",
        flow_id=flow.id,
        session_metadata={"user_id": str(other_user.id), "api_version": "v1"},
    )
    async_session.add_all([release_owner, runtime_user, other_user, flow, version, release, own_message, other_message])
    await async_session.commit()

    assert await list_sessions("v1", async_session, runtime_user) == ["own-session"]

    release.manifest = {"deployment": {"store_conversations": False}}
    async_session.add(release)
    await async_session.commit()

    assert await list_sessions("v1", async_session, runtime_user) == []


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
        {
            "subflow-id": {
                "id": "subflow-id",
                "name": "released-subflow",
                "data": {"nodes": [], "edges": []},
            }
        },
        SimplifiedAPIRequest(tweaks={"custom": {"input_value": "hello"}}),
        user,
    )

    assert payload["execution_boundary"] == "whole-flow"
    assert payload["flow"]["data"] == flow.data
    assert payload["context"]["deployment_subflows"]["subflow-id"]["name"] == "released-subflow"
    assert payload["context"]["runtime_session_metadata"] == {
        "user_id": str(user.id),
        "api_version": "v1",
        "deployment_release_id": str(release.id),
        "trigger": "api",
    }
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
    audit_event = AsyncMock()
    monkeypatch.setattr("langflow.api.v1.runtime._immutable_agent_flow", AsyncMock(return_value=(release, flow)))
    monkeypatch.setattr("langflow.api.v1.runtime._immutable_subflows", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "langflow.api.v1.runtime._contract_input",
        lambda *_args: SimplifiedAPIRequest(input_value="hello"),
    )
    monkeypatch.setattr("langflow.api.v1.runtime._run_in_sandbox", sandbox_run)
    monkeypatch.setattr("langflow.api.v1.runtime._run_flow_internal", standard_run)
    monkeypatch.setattr("langflow.api.v1.runtime._record_runtime_audit_event", audit_event)

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
    audit_event.assert_awaited_once()
    assert audit_event.call_args.kwargs["details"]["status"] == "stream_started"


async def test_cron_uses_same_immutable_agent_execution_path(async_session, monkeypatch):
    monkeypatch.setenv("UNNEST_RUNTIME_SETUP_COMPLETE", "true")
    user = User(username="schedule-owner", password="unused", is_active=True)  # noqa: S106
    flow = Flow(name="scheduled-agent", user_id=user.id, data={"nodes": [], "edges": []})
    version = FlowVersion(
        flow_id=flow.id,
        user_id=user.id,
        data={"nodes": [], "edges": []},
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
    schedule = RuntimeSchedule(
        name="nightly",
        cron_expression="0 0 * * *",
        api_version="v1",
        request_payload={"message": "scheduled"},
        next_run_at=datetime.now(timezone.utc),
    )
    async_session.add_all([user, flow, version, release, schedule])
    schedule_id = schedule.id
    await async_session.commit()
    run_agent = AsyncMock(return_value={})

    @asynccontextmanager
    async def test_session_scope():
        yield async_session

    monkeypatch.setattr("langflow.api.v1.runtime.session_scope", test_session_scope)
    monkeypatch.setattr("langflow.api.v1.runtime._run_agent", run_agent)

    await execute_scheduled_agent(schedule_id)

    run_agent.assert_awaited_once()
    assert run_agent.call_args.kwargs["payload"] == {"message": "scheduled"}
    assert run_agent.call_args.kwargs["trigger"] == "cron"
