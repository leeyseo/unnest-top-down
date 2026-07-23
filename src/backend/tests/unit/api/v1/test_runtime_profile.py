from langflow.api.v1.runtime import _immutable_agent_flow
from langflow.main import create_runtime_app
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.user.model import User
from langflow.services.deps import get_settings_service


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
    assert "/health" in paths
    assert "/ready" in paths
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
