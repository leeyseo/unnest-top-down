"""Prometheus metrics exposed by the isolated on-premise runtime."""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "unnest_runtime_requests_total",
    "Runtime HTTP requests",
    ("method", "route", "status"),
)
REQUEST_LATENCY = Histogram(
    "unnest_runtime_request_duration_seconds",
    "Runtime HTTP request latency",
    ("method", "route"),
)
QUOTA_REJECTIONS = Counter(
    "unnest_runtime_quota_rejections_total",
    "Runtime API key quota rejections",
    ("limit",),
)
INGESTION_JOBS = Gauge(
    "unnest_runtime_ingestion_jobs",
    "Runtime ingestion jobs by status",
    ("status",),
)
QUEUE_VALUES = Gauge(
    "unnest_runtime_queue_value",
    "Runtime queue metrics",
    ("metric",),
)
SETUP_COMPLETE = Gauge(
    "unnest_runtime_setup_complete",
    "Whether initial runtime setup is complete",
)
LICENSE_VALID = Gauge(
    "unnest_runtime_license_valid",
    "Whether the installed offline license is currently valid",
)


class RuntimeMetricsMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500
        recorded = False

        def record() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            route = scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            method = scope.get("method", "UNKNOWN")
            REQUESTS.labels(method, route_path, str(status_code)).inc()
            REQUEST_LATENCY.labels(method, route_path).observe(time.perf_counter() - started)

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                record()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            record()
            raise
