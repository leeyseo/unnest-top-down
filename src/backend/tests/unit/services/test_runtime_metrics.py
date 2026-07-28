from fastapi import FastAPI, Response
from httpx import ASGITransport, AsyncClient
from langflow.api.v1.runtime import runtime_metrics
from langflow.services.database.models.jobs.model import JobStatus
from langflow.services.runtime_metrics import RuntimeMetricsMiddleware
from prometheus_client import generate_latest


async def test_runtime_metrics_middleware_records_route_status_and_latency():
    app = FastAPI()
    app.add_middleware(RuntimeMetricsMiddleware)

    @app.get("/ping", status_code=204)
    async def ping() -> Response:
        return Response(status_code=204)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ping")

    assert response.status_code == 204
    metrics = generate_latest().decode()
    assert 'unnest_runtime_requests_total{method="GET",route="/ping",status="204"}' in metrics
    assert 'unnest_runtime_request_duration_seconds_count{method="GET",route="/ping"}' in metrics


async def test_runtime_metrics_endpoint_exports_ingestion_queue_and_setup(monkeypatch):
    class Result:
        def all(self):
            return [(JobStatus.QUEUED, 2)]

    class Session:
        async def exec(self, _statement):
            return Result()

    class Queue:
        def metrics_snapshot(self):
            return {"backend": "asyncio", "active_jobs": 3}

    monkeypatch.setenv("UNNEST_RUNTIME_SETUP_COMPLETE", "true")
    monkeypatch.setattr("langflow.api.v1.runtime.get_queue_service", lambda: Queue())
    monkeypatch.setattr("langflow.api.v1.runtime.runtime_license_status", lambda *_args: {"valid": True})

    response = await runtime_metrics(Session())  # type: ignore[arg-type]
    metrics = response.body.decode()

    assert 'unnest_runtime_ingestion_jobs{status="queued"} 2.0' in metrics
    assert 'unnest_runtime_queue_value{metric="active_jobs"} 3.0' in metrics
    assert "unnest_runtime_setup_complete 1.0" in metrics
    assert "unnest_runtime_license_valid 1.0" in metrics
