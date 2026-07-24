from __future__ import annotations

import base64
import hashlib
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langflow.api.v1.schemas.on_prem_deployments import OnPremDeploymentConfig
from langflow.services.deployment.build_worker import (
    BuildRequest,
    BuildWorkerConfig,
    DockerImageBuildWorker,
)
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.deployment.offline_dependencies import DependencyLockError, stage_locked_wheels


def test_release_uses_configured_pinned_base_image(monkeypatch):
    digest = f"sha256:{'a' * 64}"
    monkeypatch.setenv("UNNEST_RUNTIME_BASE_IMAGE", f"registry.internal/unnest-runtime@{digest}")

    assert OnPremDeploymentConfig().base_image_digest == digest


def test_build_request_rejects_dependency_lock_not_declared_by_flows():
    request = _request(uuid4(), f"sha256:{'a' * 64}").model_dump(mode="json")
    request["manifest"]["dependency_lock"]["python_packages"] = [
        {"name": "undeclared", "version": "1.0.0", "hashes": [f"sha256:{'b' * 64}"]}
    ]

    with pytest.raises(ValueError, match="does not match the immutable Flow declarations"):
        BuildRequest.model_validate(request)


def _license_materials(root: Path, release_digest: str) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    license_blob = json.dumps(
        {
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "release_digest": release_digest,
        },
        separators=(",", ":"),
    ).encode()
    for relative in ("license/license.json", "license/license.sig"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / "license/license.json").write_bytes(license_blob)
    (root / "license/license.sig").write_text(
        base64.b64encode(private_key.sign(license_blob)).decode(),
        encoding="utf-8",
    )
    public_key = root / "vendor-license.pem"
    public_key.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return root, public_key


def _request(build_id, base_digest: str) -> BuildRequest:
    agent_id, ingestion_id = uuid4(), uuid4()
    agent_flow_id, ingestion_flow_id = uuid4(), uuid4()
    agent = {"nodes": [], "edges": []}
    ingestion = {"nodes": [], "edges": []}
    flows = [
        {
            "id": str(agent_id),
            "flow_id": str(agent_flow_id),
            "version_number": 1,
            "digest": canonical_digest(agent),
            "role": "agent",
            "declared_dependencies": {"python_packages": [], "os_packages": [], "binaries": []},
        },
        {
            "id": str(ingestion_id),
            "flow_id": str(ingestion_flow_id),
            "version_number": 1,
            "digest": canonical_digest(ingestion),
            "role": "ingestion",
            "declared_dependencies": {"python_packages": [], "os_packages": [], "binaries": []},
        },
    ]
    manifest = {
        "provider": "unnest-on-prem",
        "release_version": "1.2.3",
        "release_digest": canonical_digest(flows),
        "flows": flows,
        "api": {"version": "v1", "openapi": {"openapi": "3.1.0"}},
        "deployment": {
            "architecture": "amd64",
            "orchestrator": "compose",
            "topology": "single",
            "accelerator": "cpu",
            "database": "embedded-postgresql",
            "storage": "local",
            "infrastructure": "bundled",
            "model_runtime": "external",
        },
        "build": {
            "architecture": "amd64",
            "base_image_digest": base_digest,
            "signing_enabled": True,
            "signer_fingerprint": None,
        },
        "acceptance_tests": [
            {
                "name": "health",
                "required": True,
                "request": {"path": "/health"},
                "expected": {"status": 200},
            }
        ],
        "knowledge_base_alias": "shared",
        "dependency_lock": {"python_packages": [], "os_packages": [], "binaries": []},
        "sandbox": {"required": False},
        "package": {
            "layout_version": 2,
            "required_files": [
                "manifest/release.json",
                "openapi/openapi.json",
                "reports/sbom.cdx.json",
                "reports/trivy.json",
                "tests/acceptance.json",
                "license/license.json",
                "license/license.sig",
                "compose/compose.yml",
                "signatures/checksums.sig",
            ],
            "required_globs": ["flows/*.json", "images/*.tar"],
        },
    }
    return BuildRequest(
        release_id=uuid4(),
        build_id=build_id,
        manifest=manifest,
        flows=[
            {"version_id": agent_id, "data": agent},
            {"version_id": ingestion_id, "data": ingestion},
        ],
        critical_override=None,
        reproducible={"source_date_epoch": 0, "sort_files": True},
    )


def test_worker_builds_reproducible_docker_image_tar(tmp_path, monkeypatch):
    digest = f"sha256:{'a' * 64}"
    first_request = _request(uuid4(), digest)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "agency_sdk-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"locked offline wheel")
    wheel_digest = f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
    dependency_lock = {
        "python_packages": [{"name": "agency-sdk", "version": "1.2.3", "hashes": [wheel_digest]}],
        "os_packages": [],
        "binaries": [],
    }
    first_request.manifest["dependency_lock"] = dependency_lock
    first_request.manifest["flows"][0]["declared_dependencies"] = dependency_lock
    license_materials, license_public_key = _license_materials(
        tmp_path / "materials",
        first_request.manifest["release_digest"],
    )
    support_images = tmp_path / "support-images"
    support_images.mkdir()
    (support_images / "postgresql.tar").write_bytes(b"postgresql image")
    (support_images / "redis.tar").write_bytes(b"redis image")
    signing_key = Ed25519PrivateKey.generate()
    cosign_key = tmp_path / "cosign.key"
    cosign_key.write_bytes(b"test private key placeholder")
    cosign_public_key = tmp_path / "cosign.pub"
    cosign_public_key.write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    tls = tmp_path / "tls"
    tls.mkdir()
    for name in ("ca.pem", "cert.pem", "key.pem"):
        (tls / name).touch()
    config = BuildWorkerConfig(
        root=tmp_path / "jobs",
        buildkit_addr="tcp://buildkit.internal:1234",
        buildkit_ca=tls / "ca.pem",
        buildkit_cert=tls / "cert.pem",
        buildkit_key=tls / "key.pem",
        runtime_base_image=f"registry.internal/unnest-runtime@{digest}",
        license_materials=license_materials,
        license_public_key=license_public_key,
        support_images=support_images,
        postgres_image=f"docker.io/library/postgres:16@sha256:{'c' * 64}",
        redis_image=f"docker.io/library/redis:7@sha256:{'d' * 64}",
        cosign_key=cosign_key,
        cosign_public_key=cosign_public_key,
        wheelhouse=wheelhouse,
    )
    worker = DockerImageBuildWorker(config)
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path, env=None) -> str:
        del cwd, env
        commands.append(command)
        if command[0] == "buildctl":
            output = command[command.index("--output") + 1]
            destination = next(value.removeprefix("dest=") for value in output.split(",") if value.startswith("dest="))
            Path(destination).write_bytes(b"deterministic docker image")
            metadata = Path(command[command.index("--metadata-file") + 1])
            metadata.write_text(json.dumps({"containerimage.digest": f"sha256:{'b' * 64}"}), encoding="utf-8")
        elif command[0] == "trivy":
            destination = Path(command[command.index("--output") + 1])
            if "cyclonedx" in command:
                destination.write_text(json.dumps({"bomFormat": "CycloneDX", "components": []}), encoding="utf-8")
            else:
                destination.write_text(json.dumps({"Results": []}), encoding="utf-8")
        elif command[0] == "cosign":
            destination = Path(command[command.index("--output-signature") + 1])
            blob = Path(command[-1]).read_bytes()
            destination.write_bytes(base64.b64encode(signing_key.sign(blob)))
        elif command[:2] == ["skopeo", "inspect"]:
            archive = command[-1]
            character = "b" if "unnest-runtime" in archive else "c" if "postgresql" in archive else "d"
            return f"sha256:{character * 64}"
        return ""

    monkeypatch.setattr(worker, "_run", fake_run)
    worker.submit(first_request)
    worker.run(str(first_request.build_id))
    first = worker._status(str(first_request.build_id))

    second_request = first_request.model_copy(update={"build_id": uuid4()})
    worker.submit(second_request)
    worker.run(str(second_request.build_id))
    second = worker._status(str(second_request.build_id))

    assert first.status == second.status == "succeeded", (first.logs, second.logs)
    assert first.artifacts[0].artifact_type == "package"
    assert first.artifacts[0].digest == second.artifacts[0].digest
    with tarfile.open(worker.artifact(str(first_request.build_id)), "r:") as package:
        names = set(package.getnames())
        assert {
            "manifest/release.json",
            "compose/compose.yml",
            "signatures/checksums.sig",
            "wheels/agency_sdk-1.2.3-py3-none-any.whl",
            "wheels/requirements.lock",
        }.issubset(names)
    assert [command[0] for command in commands].count("buildctl") == 2
    assert [command[0] for command in commands].count("trivy") == 12
    assert [command[0] for command in commands].count("cosign") == 2
    assert [command[0] for command in commands].count("skopeo") == 6
    assert worker._scan_findings(
        {"Results": [{"Secrets": [{"Severity": "HIGH", "RuleID": "embedded-api-key"}]}]}
    ) == ["secret:embedded-api-key"]

    missing_request = first_request.model_copy(deep=True)
    missing_hash = f"sha256:{'e' * 64}"
    missing_request.manifest["dependency_lock"]["python_packages"][0]["hashes"] = [missing_hash]
    missing_request.manifest["flows"][0]["declared_dependencies"]["python_packages"][0]["hashes"] = [
        missing_hash
    ]
    missing_job = tmp_path / "missing-wheel-job"
    missing_job.mkdir()
    with pytest.raises(DependencyLockError, match="does not satisfy the complete lock"):
        stage_locked_wheels(config.wheelhouse, missing_job / "locked-wheels", missing_request.manifest)
