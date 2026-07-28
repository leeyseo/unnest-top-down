from __future__ import annotations

import base64
import hashlib
import json
import tarfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langflow.api.v1.schemas.on_prem_deployments import OnPremDeploymentConfig
from langflow.services.deployment.build_worker import (
    BuildCapacityError,
    BuildRequest,
    BuildWorkerConfig,
    DockerImageBuildWorker,
    RegistryPushRequest,
    create_build_worker_app,
)
from langflow.services.deployment.buildkit import BuildKitWorkerClient, WorkerBuildStatus
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.deployment.offline_dependencies import DependencyLockError, stage_locked_wheels
from langflow.services.deployment.source_documents import SourceDocumentError


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


def test_build_request_accepts_release_requiring_whole_flow_sandbox():
    request = _request(uuid4(), f"sha256:{'a' * 64}").model_dump(mode="json")
    request["manifest"]["sandbox"] = {
        "required": True,
        "network_policy": "deny-by-default",
        "allowed_endpoints": ["https://models.internal/v1"],
    }

    assert BuildRequest.model_validate(request).manifest["sandbox"]["required"] is True


def test_worker_limits_active_builds(tmp_path):
    worker = object.__new__(DockerImageBuildWorker)
    worker.config = SimpleNamespace(root=tmp_path, max_concurrent_builds=1)
    worker._build_lock = threading.Lock()
    worker._active_builds = set()
    worker._running_builds = set()
    worker.submit(_request(uuid4(), f"sha256:{'a' * 64}"))

    with pytest.raises(BuildCapacityError, match="at capacity"):
        worker.submit(_request(uuid4(), f"sha256:{'a' * 64}"))


def test_registry_push_rejects_repository_outside_credential_allowlist(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials"
    credential = credentials / "PRODUCTION"
    credential.mkdir(parents=True)
    (credential / "config.json").write_text("{}", encoding="utf-8")
    (credential / "repositories.txt").write_text("registry.internal/approved/unnest\n", encoding="utf-8")
    worker = object.__new__(DockerImageBuildWorker)
    worker.config = SimpleNamespace(registry_credentials=credentials)
    monkeypatch.setattr(
        worker,
        "_status",
        lambda _job_id: WorkerBuildStatus(
            job_id=str(uuid4()),
            status="succeeded",
            scan_report={"critical": 0},
            artifacts=[
                {
                    "artifact_type": "package",
                    "location": "release.tar",
                    "digest": f"sha256:{'a' * 64}",
                    "checksums": {"release.tar": f"sha256:{'a' * 64}"},
                    "sbom": {"bomFormat": "CycloneDX"},
                }
            ],
        ),
    )

    with pytest.raises(RuntimeError, match="not allowed"):
        worker.push_registry(
            str(uuid4()),
            RegistryPushRequest(
                reference="registry.internal/unapproved/unnest:1.0.0",
                credential_secret_name="PRODUCTION",  # noqa: S106
            ),
        )


async def test_mtls_client_and_worker_transfer_source_documents_as_multipart(tmp_path):
    source_id = uuid4()
    contents = b"source bytes crossing the control-plane worker boundary"
    source = tmp_path / "source"
    source.write_bytes(contents)
    request = _request(uuid4(), f"sha256:{'a' * 64}")
    request.manifest["source_documents"] = [
        {
            "id": str(source_id),
            "name": "guide.txt",
            "size_bytes": len(contents),
            "digest": f"sha256:{hashlib.sha256(contents).hexdigest()}",
            "mime_type": "text/plain",
            "package_path": f"documents/source/{source_id}/guide.txt",
        }
    ]

    class Worker:
        received: tuple[str, bytes] | None = None

        def submit(self, submitted, source_documents):
            assert submitted == request
            path = source_documents / str(source_id)
            self.received = (path.name, path.read_bytes())
            return WorkerBuildStatus(job_id=str(submitted.build_id), status="failed", logs="test stop")

    worker = Worker()
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_build_worker_app(worker)),  # type: ignore[arg-type]
        base_url="https://worker.internal",
    )
    client = object.__new__(BuildKitWorkerClient)
    client._client = http_client
    try:
        result = await client.submit(
            request.model_dump(mode="json"),
            [(str(source_id), source)],
        )
    finally:
        await http_client.aclose()

    assert result.status == "failed"
    assert worker.received == (str(source_id), contents)


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
    source_id = uuid4()
    source_contents = b"government deployment source document"
    source_uploads = tmp_path / "source-uploads"
    source_uploads.mkdir()
    (source_uploads / str(source_id)).write_bytes(source_contents)
    first_request.manifest["source_documents"] = [
        {
            "id": str(source_id),
            "name": "guide.txt",
            "size_bytes": len(source_contents),
            "digest": f"sha256:{hashlib.sha256(source_contents).hexdigest()}",
            "mime_type": "text/plain",
            "package_path": f"documents/source/{source_id}/guide.txt",
        }
    ]
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
    dockerfiles: list[str] = []

    def fake_run(command: list[str], *, cwd: Path, env=None) -> str:
        del cwd, env
        commands.append(command)
        if command[0] == "buildctl":
            context_argument = next(value for value in command if value.startswith("context="))
            dockerfiles.append(
                (Path(context_argument.removeprefix("context=")) / "Dockerfile").read_text(encoding="utf-8")
            )
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
    worker.submit(first_request, source_uploads)
    worker.run(str(first_request.build_id))
    first = worker._status(str(first_request.build_id))

    second_request = first_request.model_copy(update={"build_id": uuid4()})
    worker.submit(second_request, source_uploads)
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
            f"documents/source/{source_id}/guide.txt",
        }.issubset(names)
        compose = package.extractfile("compose/compose.yml")
        assert compose is not None
        compose_text = compose.read().decode()
        compose_config = yaml.safe_load(compose_text)
        restore_service = compose_config["services"]["restore"]
        assert restore_service["profiles"] == ["maintenance"]
        assert restore_service["user"] == "1000:0"
        assert restore_service["read_only"] is True
        assert restore_service["cap_drop"] == ["ALL"]
        assert "sandbox-controller:" in compose_text
        assert "sandbox-executor:" in compose_text
        assert "restore:" in compose_text
        assert 'profiles: ["maintenance"]' in compose_text
        assert 'user: "1000:0"' in compose_text
        assert compose_text.count("group_add:") == 3
        assert compose_text.count('"${UNNEST_INSTALL_GID}"') == 3
        assert compose_text.count('UNNEST_SANDBOX_EXECUTION_TIMEOUT_SECONDS: "300"') == 2
        assert compose_text.count('LANGFLOW_MAX_FILE_SIZE_UPLOAD: "512"') == 2
        assert compose_text.count("TMPDIR: /app/langflow") == 2
        assert compose_text.count(":ro,Z") == 3
        assert "read_only: true" in compose_text
        assert "no-new-privileges:true" in compose_text
        assert "sandbox-control:" in compose_text
        assert "sandbox-egress:" in compose_text
        assert compose_text.count("internal: true") == 3
    assert all("command -v pg_dump" in dockerfile for dockerfile in dockerfiles)
    assert all("command -v pg_restore" in dockerfile for dockerfile in dockerfiles)
    assert [command[0] for command in commands].count("buildctl") == 2
    assert [command[0] for command in commands].count("trivy") == 12
    assert [command[0] for command in commands].count("cosign") == 2
    assert [command[0] for command in commands].count("skopeo") == 6
    assert worker._scan_findings({"Results": [{"Secrets": [{"Severity": "HIGH", "RuleID": "embedded-api-key"}]}]}) == [
        "secret:embedded-api-key"
    ]

    (source_uploads / str(source_id)).write_bytes(b"tampered")
    tampered_request = first_request.model_copy(update={"build_id": uuid4()})
    with pytest.raises(SourceDocumentError, match="does not match its snapshot"):
        worker.submit(tampered_request, source_uploads)
    assert not (config.root / str(tampered_request.build_id)).exists()

    missing_request = first_request.model_copy(deep=True)
    missing_hash = f"sha256:{'e' * 64}"
    missing_request.manifest["dependency_lock"]["python_packages"][0]["hashes"] = [missing_hash]
    missing_request.manifest["flows"][0]["declared_dependencies"]["python_packages"][0]["hashes"] = [missing_hash]
    missing_job = tmp_path / "missing-wheel-job"
    missing_job.mkdir()
    with pytest.raises(DependencyLockError, match="does not satisfy the complete lock"):
        stage_locked_wheels(config.wheelhouse, missing_job / "locked-wheels", missing_request.manifest)
