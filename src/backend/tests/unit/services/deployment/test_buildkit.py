from __future__ import annotations

import pytest
from langflow.services.deployment.buildkit import (
    BuildKitWorkerClient,
    WorkerBuildStatus,
    sanitize_flow_for_build,
)
from pydantic import ValidationError


def test_sanitize_flow_for_build_removes_literal_passwords():
    flow = {
        "nodes": [
            {
                "data": {
                    "node": {
                        "template": {
                            "literal": {"password": True, "load_from_db": False, "value": "secret"},
                            "reference": {"password": True, "load_from_db": True, "value": "MODEL_API_KEY"},
                        }
                    }
                }
            }
        ],
        "edges": [],
    }

    sanitized = sanitize_flow_for_build(flow)

    template = sanitized["nodes"][0]["data"]["node"]["template"]
    assert template["literal"]["value"] is None
    assert template["reference"]["value"] == "MODEL_API_KEY"
    assert flow["nodes"][0]["data"]["node"]["template"]["literal"]["value"] == "secret"


def test_worker_client_rejects_non_tls_url(tmp_path):
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    for path in (ca, cert, key):
        path.touch()

    with pytest.raises(ValueError, match="HTTPS"):
        BuildKitWorkerClient(base_url="http://worker.internal", ca=ca, cert=cert, key=key)


def test_successful_worker_status_requires_artifacts():
    with pytest.raises(ValidationError, match="must include artifacts"):
        WorkerBuildStatus(job_id="job-1", status="succeeded")


def test_successful_worker_status_requires_scan_report():
    with pytest.raises(ValidationError, match="security scan report"):
        WorkerBuildStatus(
            job_id="job-1",
            status="succeeded",
            artifacts=[
                {
                    "artifact_type": "tar",
                    "location": "artifact.tar",
                    "digest": f"sha256:{'a' * 64}",
                    "checksums": {"artifact.tar": f"sha256:{'a' * 64}"},
                    "sbom": {"bomFormat": "CycloneDX"},
                }
            ],
        )
