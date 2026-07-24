"""Isolated worker that exports an immutable release as a signed offline package."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from langflow.services.deployment.buildkit import WorkerArtifact, WorkerBuildStatus
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.deployment.offline_package import create_reproducible_tar, sha256_file, write_checksums
from langflow.unnestctl import public_key_fingerprint, verify_license, verify_package

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REGISTRY_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]+$")
_DOCKERFILE = """# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0
COPY --chown=1000:0 bundle /opt/unnest
ENV UNNEST_RELEASE_BUNDLE=/opt/unnest/release
ENV DO_NOT_TRACK=true LANGFLOW_DO_NOT_TRACK=true
RUN python -c "import langflow,pathlib; \
p=pathlib.Path(langflow.__file__).parent/'frontend/.unnest-runtime-profile'; assert p.is_file()"
USER 1000
CMD ["uvicorn","langflow.main:create_runtime_app","--factory","--host","0.0.0.0","--port","7860"]
"""
_COMPOSE_TEMPLATE = """name: unnest
services:
  postgresql:
    image: {postgres_image}
    pull_policy: never
    environment:
      POSTGRES_DB: unnest
      POSTGRES_USER: unnest
      POSTGRES_PASSWORD: ${{UNNEST_DB_PASSWORD:?UNNEST_DB_PASSWORD is required}}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U unnest -d unnest"]
      interval: 5s
      timeout: 5s
      retries: 30
    restart: unless-stopped

  redis:
    image: {redis_image}
    pull_policy: never
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 30
    restart: unless-stopped

  runtime:
    image: {runtime_image}
    pull_policy: never
    depends_on:
      postgresql:
        condition: service_healthy
      redis:
        condition: service_healthy
    command:
      - uvicorn
      - langflow.main:create_runtime_app
      - --factory
      - --host
      - 0.0.0.0
      - --port
      - "7860"
      - --ssl-keyfile
      - /opt/unnest/tls/server.key
      - --ssl-certfile
      - /opt/unnest/tls/server.crt
    environment:
      LANGFLOW_DATABASE_URL: postgresql://unnest:${{UNNEST_DB_PASSWORD}}@postgresql:5432/unnest
      LANGFLOW_DATABASE_CONNECTION_RETRY: "true"
      LANGFLOW_CONFIG_DIR: /app/langflow
      LANGFLOW_KNOWLEDGE_BASES_DIR: /app/langflow/knowledge_bases
      LANGFLOW_STORAGE_TYPE: local
      LANGFLOW_CACHE_TYPE: redis
      LANGFLOW_REDIS_URL: redis://redis:6379/0
      LANGFLOW_JOB_QUEUE_TYPE: redis
      LANGFLOW_REDIS_QUEUE_URL: redis://redis:6379/1
      LANGFLOW_CELERY_ENABLED: "false"
      LANGFLOW_AUTO_LOGIN: "false"
      UNNEST_MASTER_KEY_FILE: /app/langflow/secrets/master.key
      UNNEST_BACKUP_DIR: /app/langflow/backups
      DO_NOT_TRACK: "true"
      LANGFLOW_DO_NOT_TRACK: "true"
    ports:
      - "7860:7860"
    volumes:
      - runtime-data:/app/langflow
      - ./tls:/opt/unnest/tls:ro
    read_only: true
    tmpfs:
      - /tmp:size=256m,mode=1777
    restart: unless-stopped

volumes:
  runtime-data:
  postgres-data:
  redis-data:
"""


def _image_tag(reference: str) -> str:
    tag = reference.rpartition("@")[0]
    if ":" not in tag.rsplit("/", 1)[-1]:
        msg = "Pinned support image references must also include an offline tag"
        raise ValueError(msg)
    return tag


class BuildFlow(BaseModel):
    model_config = {"extra": "forbid"}

    version_id: UUID
    data: dict[str, Any]


class BuildRequest(BaseModel):
    model_config = {"extra": "forbid"}

    release_id: UUID
    build_id: UUID
    manifest: dict[str, Any]
    flows: list[BuildFlow] = Field(min_length=2)
    critical_override: dict[str, str] | None = None
    reproducible: dict[str, Any]

    @model_validator(mode="after")
    def valid_release(self) -> BuildRequest:
        if self.manifest.get("provider") != "unnest-on-prem":
            msg = "Unsupported deployment provider"
            raise ValueError(msg)
        entries = self.manifest.get("flows")
        if not isinstance(entries, list):
            msg = "Release manifest does not declare flows"
            raise TypeError(msg)
        expected = {str(entry.get("id")): entry for entry in entries if isinstance(entry, dict)}
        supplied = {str(flow.version_id): flow for flow in self.flows}
        if set(expected) != set(supplied):
            msg = "Build flows do not match the release manifest"
            raise ValueError(msg)
        for version_id, flow in supplied.items():
            if canonical_digest(flow.data) != expected[version_id].get("digest"):
                msg = f"Build Flow Version digest mismatch: {version_id}"
                raise ValueError(msg)
        if self.reproducible != {"source_date_epoch": 0, "sort_files": True}:
            msg = "Build must request the supported reproducible settings"
            raise ValueError(msg)
        if self.manifest.get("build", {}).get("signing_enabled") is not True:
            msg = "Offline package signing is mandatory"
            raise ValueError(msg)
        deployment = self.manifest.get("deployment", {})
        if (
            deployment.get("orchestrator") != "compose"
            or deployment.get("topology") != "single"
            or deployment.get("architecture") != "amd64"
        ):
            msg = "Build request is not an approved offline MVP profile"
            raise ValueError(msg)
        if self.manifest.get("sandbox", {}).get("required"):
            msg = "Risky Flow export requires the sandbox worker, which is not available"
            raise ValueError(msg)
        return self


class RegistryPushRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reference: str = Field(min_length=1, max_length=512)
    credential_secret_name: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def safe_values(self) -> RegistryPushRequest:
        if not _REGISTRY_REFERENCE_RE.fullmatch(self.reference) or "://" in self.reference:
            msg = "Registry reference is invalid"
            raise ValueError(msg)
        if not _SECRET_NAME_RE.fullmatch(self.credential_secret_name):
            msg = "Registry credential secret name is invalid"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class BuildWorkerConfig:
    root: Path
    buildkit_addr: str
    buildkit_ca: Path
    buildkit_cert: Path
    buildkit_key: Path
    runtime_base_image: str
    license_materials: Path
    license_public_key: Path
    support_images: Path
    postgres_image: str
    redis_image: str
    cosign_key: Path
    cosign_public_key: Path
    registry_credentials: Path | None = None
    forbidden_licenses: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> BuildWorkerConfig:
        required = {
            "root": os.getenv("UNNEST_BUILD_WORKER_ROOT"),
            "buildkit_addr": os.getenv("UNNEST_BUILDKIT_ADDR"),
            "buildkit_ca": os.getenv("UNNEST_BUILDKIT_CA"),
            "buildkit_cert": os.getenv("UNNEST_BUILDKIT_CERT"),
            "buildkit_key": os.getenv("UNNEST_BUILDKIT_KEY"),
            "runtime_base_image": os.getenv("UNNEST_RUNTIME_BASE_IMAGE"),
            "license_materials": os.getenv("UNNEST_LICENSE_MATERIALS"),
            "license_public_key": os.getenv("UNNEST_LICENSE_PUBLIC_KEY"),
            "support_images": os.getenv("UNNEST_SUPPORT_IMAGES"),
            "postgres_image": os.getenv("UNNEST_POSTGRES_IMAGE"),
            "redis_image": os.getenv("UNNEST_REDIS_IMAGE"),
            "cosign_key": os.getenv("UNNEST_COSIGN_KEY"),
            "cosign_public_key": os.getenv("UNNEST_COSIGN_PUBLIC_KEY"),
        }
        if missing := [name for name, value in required.items() if not value]:
            msg = f"Build worker configuration is incomplete: {', '.join(missing)}"
            raise ValueError(msg)
        root = Path(str(required["root"])).resolve()
        if "," in str(root):
            msg = "Build worker root must not contain commas"
            raise ValueError(msg)
        config = cls(
            root=root,
            buildkit_addr=str(required["buildkit_addr"]),
            buildkit_ca=Path(str(required["buildkit_ca"])),
            buildkit_cert=Path(str(required["buildkit_cert"])),
            buildkit_key=Path(str(required["buildkit_key"])),
            runtime_base_image=str(required["runtime_base_image"]),
            license_materials=Path(str(required["license_materials"])),
            license_public_key=Path(str(required["license_public_key"])),
            support_images=Path(str(required["support_images"])),
            postgres_image=str(required["postgres_image"]),
            redis_image=str(required["redis_image"]),
            cosign_key=Path(str(required["cosign_key"])),
            cosign_public_key=Path(str(required["cosign_public_key"])),
            registry_credentials=(
                Path(value).resolve() if (value := os.getenv("UNNEST_REGISTRY_CREDENTIALS")) else None
            ),
            forbidden_licenses=frozenset(
                item.strip().casefold()
                for item in os.getenv("UNNEST_FORBIDDEN_LICENSES", "").split(",")
                if item.strip()
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.buildkit_addr.startswith("tcp://"):
            msg = "BuildKit address must use remote TCP transport"
            raise ValueError(msg)
        for label, path in (
            ("BuildKit CA", self.buildkit_ca),
            ("BuildKit certificate", self.buildkit_cert),
            ("BuildKit key", self.buildkit_key),
            ("license directory", self.license_materials),
            ("vendor license public key", self.license_public_key),
            ("support image directory", self.support_images),
            ("Cosign key", self.cosign_key),
            ("Cosign public key", self.cosign_public_key),
        ):
            is_directory = label in {"license directory", "support image directory"}
            if (is_directory and not path.is_dir()) or (
                not is_directory and not path.is_file()
            ):
                msg = f"{label} does not exist: {path}"
                raise ValueError(msg)
        for label, reference in (
            ("Runtime base", self.runtime_base_image),
            ("PostgreSQL", self.postgres_image),
            ("Redis", self.redis_image),
        ):
            if not _SHA256_RE.fullmatch(reference.rpartition("@")[2]):
                msg = f"{label} image must be pinned as repository@sha256:digest"
                raise ValueError(msg)
        _image_tag(self.postgres_image)
        _image_tag(self.redis_image)
        for name in ("postgresql.tar", "redis.tar"):
            image = self.support_images / name
            if not image.is_file() or image.is_symlink():
                msg = f"Support image archive is missing: {name}"
                raise ValueError(msg)


class DockerImageBuildWorker:
    def __init__(self, config: BuildWorkerConfig) -> None:
        config.validate()
        self.config = config
        self.config.root.mkdir(parents=True, exist_ok=True)

    def _job(self, job_id: str) -> Path:
        try:
            normalized = str(UUID(job_id))
        except ValueError as exc:
            msg = "Invalid build job id"
            raise KeyError(msg) from exc
        job = self.config.root / normalized
        if job.is_symlink():
            msg = "Build job path is invalid"
            raise KeyError(msg)
        return job

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _status(self, job_id: str) -> WorkerBuildStatus:
        try:
            value = json.loads((self._job(job_id) / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            msg = "Build job not found"
            raise KeyError(msg) from exc
        return WorkerBuildStatus.model_validate(value)

    def submit(self, request: BuildRequest) -> WorkerBuildStatus:
        job = self._job(str(request.build_id))
        if (job / "status.json").is_file():
            current = self._status(str(request.build_id))
            if current.status in {"queued", "running", "succeeded"}:
                return current
            shutil.rmtree(job)
        job.mkdir(parents=True)
        self._write_json(job / "request.json", request.model_dump(mode="json"))
        queued = WorkerBuildStatus(job_id=str(request.build_id), status="queued")
        self._write_json(job / "status.json", queued.model_dump(mode="json"))
        return queued

    def _run(self, command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        if completed.returncode:
            msg = output or f"{command[0]} exited with status {completed.returncode}"
            raise RuntimeError(msg)
        return output

    def _prepare_context(self, job: Path, request: BuildRequest) -> Path:
        context = job / "context"
        release = context / "bundle" / "release"
        self._write_json(release / "manifest" / "release.json", request.manifest)
        self._write_json(release / "openapi" / "openapi.json", request.manifest["api"]["openapi"])
        for flow in request.flows:
            self._write_json(release / "flows" / f"{flow.version_id}.json", flow.data)

        materials = context / "bundle"
        for relative in ("license/license.json", "license/license.sig"):
            source = self.config.license_materials / relative
            if not source.is_file() or source.is_symlink():
                msg = f"Required signed license material is missing: {relative}"
                raise RuntimeError(msg)
            destination = materials / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        vendor_key = materials / "trust" / "vendor-license.pem"
        vendor_key.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.config.license_public_key, vendor_key)
        verify_license(materials, request.manifest, self.config.license_public_key)

        (context / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
        for path in context.rglob("*"):
            if path.is_file():
                os.utime(path, (0, 0))
        return context

    def _scan_findings(self, report: dict[str, Any]) -> list[str]:
        findings: list[str] = []
        for result in report.get("Results", []):
            if not isinstance(result, dict):
                continue
            for group in ("Vulnerabilities", "Misconfigurations"):
                findings.extend(
                    str(item.get("VulnerabilityID") or item.get("ID") or item.get("Name") or group)
                    for item in result.get(group) or []
                    if isinstance(item, dict) and str(item.get("Severity", "")).upper() == "CRITICAL"
                )
            findings.extend(
                f"secret:{item.get('RuleID') or item.get('ID') or item.get('Title') or 'detected'}"
                for item in result.get("Secrets") or []
                if isinstance(item, dict)
            )
            findings.extend(
                f"forbidden-license:{item['Name']}"
                for item in result.get("Licenses") or []
                if isinstance(item, dict)
                and isinstance(item.get("Name"), str)
                and item["Name"].casefold() in self.config.forbidden_licenses
            )
        return sorted(set(findings))

    def _verify_image_archive(self, job: Path, image: Path, expected_digest: str) -> str:
        actual_digest = self._run(
            ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker-archive:{image}"],
            cwd=job,
        ).strip()
        if actual_digest != expected_digest:
            msg = f"Image archive digest mismatch: {image.name}"
            raise RuntimeError(msg)
        return f"verified image archive {image.name} at {actual_digest}"

    def _scan_image(self, job: Path, *, name: str, image: Path, image_digest: str) -> tuple[dict, dict, list[str]]:
        sbom_path = job / f"sbom-{name}.cdx.json"
        report_path = job / f"trivy-{name}.json"
        logs = [
            self._run(
                ["trivy", "image", "--input", str(image), "--format", "cyclonedx", "--output", str(sbom_path)],
                cwd=job,
            ),
            self._run(
                [
                    "trivy",
                    "image",
                    "--input",
                    str(image),
                    "--scanners",
                    "vuln,secret,misconfig,license",
                    "--format",
                    "json",
                    "--output",
                    str(report_path),
                ],
                cwd=job,
            ),
        ]
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["image_digest"] = image_digest
        report["archive_digest"] = f"sha256:{sha256_file(image)}"
        return sbom, report, logs

    def _stage_package(
        self,
        job: Path,
        request: BuildRequest,
        *,
        runtime_image: Path,
        runtime_image_digest: str,
        sboms: dict[str, dict],
        scan_report: dict[str, Any],
    ) -> tuple[Path, str]:
        package = job / "package"
        package.mkdir()
        image_sources = {
            "unnest-runtime.tar": (
                runtime_image,
                runtime_image_digest,
                f"unnest-runtime:{request.manifest['release_version']}-amd64@{runtime_image_digest}",
            ),
            "postgresql.tar": (
                self.config.support_images / "postgresql.tar",
                self.config.postgres_image.rpartition("@")[2],
                self.config.postgres_image,
            ),
            "redis.tar": (
                self.config.support_images / "redis.tar",
                self.config.redis_image.rpartition("@")[2],
                self.config.redis_image,
            ),
        }
        images: list[dict[str, str]] = []
        for archive_name, (source, image_digest, reference) in image_sources.items():
            destination = package / "images" / archive_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            images.append(
                {
                    "archive": f"images/{archive_name}",
                    "archive_digest": f"sha256:{sha256_file(destination)}",
                    "image_digest": image_digest,
                    "reference": reference,
                }
            )

        manifest = copy.deepcopy(request.manifest)
        manifest["images"] = images
        manifest["build"]["runtime_image_digest"] = runtime_image_digest
        self._write_json(package / "manifest" / "release.json", manifest)
        self._write_json(package / "openapi" / "openapi.json", manifest["api"]["openapi"])
        self._write_json(package / "tests" / "acceptance.json", manifest["acceptance_tests"])
        for flow in request.flows:
            self._write_json(package / "flows" / f"{flow.version_id}.json", flow.data)

        compose = _COMPOSE_TEMPLATE.format(
            postgres_image=_image_tag(self.config.postgres_image),
            redis_image=_image_tag(self.config.redis_image),
            runtime_image=f"unnest-runtime:{manifest['release_version']}-amd64",
        )
        compose_path = package / "compose" / "compose.yml"
        compose_path.parent.mkdir(parents=True)
        compose_path.write_text(compose, encoding="utf-8", newline="\n")

        for relative in ("license/license.json", "license/license.sig"):
            destination = package / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.config.license_materials / relative, destination)
        self._write_json(package / "reports" / "sbom.cdx.json", sboms["runtime"])
        self._write_json(package / "reports" / "sbom-postgresql.cdx.json", sboms["postgresql"])
        self._write_json(package / "reports" / "sbom-redis.cdx.json", sboms["redis"])
        self._write_json(package / "reports" / "trivy.json", scan_report)

        write_checksums(package)
        signature_path = package / "signatures" / "checksums.sig"
        signature_path.parent.mkdir(parents=True)
        self._run(
            [
                "cosign",
                "sign-blob",
                "--yes",
                "--tlog-upload=false",
                "--key",
                str(self.config.cosign_key),
                "--output-signature",
                str(signature_path),
                str(package / "checksums.sha256"),
            ],
            cwd=job,
        )
        signature = signature_path.read_text(encoding="utf-8").strip()
        base64.b64decode(signature, validate=True)

        fingerprint = manifest["build"]["signer_fingerprint"].removeprefix("sha256:")
        trust_directory = job / "release-trust"
        trust_directory.mkdir()
        shutil.copyfile(self.config.cosign_public_key, trust_directory / f"{fingerprint}.pem")
        verify_package(
            package,
            release_trust_directory=trust_directory,
            license_public_key=self.config.license_public_key,
        )
        archive = job / f"unnest-{manifest['release_version']}-rocky9-amd64.tar"
        create_reproducible_tar(package, archive)
        return archive, signature

    def run(self, job_id: str) -> None:
        job = self._job(job_id)
        logs: list[str] = []
        running = WorkerBuildStatus(job_id=job_id, status="running")
        self._write_json(job / "status.json", running.model_dump(mode="json"))
        try:
            request = BuildRequest.model_validate_json((job / "request.json").read_text(encoding="utf-8"))
            signer_fingerprint = public_key_fingerprint(self.config.cosign_public_key.read_bytes())
            request.manifest["build"]["signer_fingerprint"] = signer_fingerprint
            context = self._prepare_context(job, request)
            image = job / "unnest-runtime.tar"
            metadata = job / "build-metadata.json"
            architecture = str(request.manifest["build"]["architecture"])
            declared_base_digest = request.manifest["build"].get("base_image_digest")
            actual_base_digest = self.config.runtime_base_image.rpartition("@")[2]
            if not isinstance(declared_base_digest, str) or not _SHA256_RE.fullmatch(declared_base_digest):
                msg = "Release manifest does not pin the Runtime base image digest"
                raise RuntimeError(msg)
            if declared_base_digest != actual_base_digest:
                msg = "Configured Runtime base image does not match the release manifest digest"
                raise RuntimeError(msg)
            image_name = f"unnest-runtime:{request.manifest['release_version']}-{architecture}"
            logs.append(
                self._run(
                    [
                        "buildctl",
                        "--addr",
                        self.config.buildkit_addr,
                        "--tlscacert",
                        str(self.config.buildkit_ca),
                        "--tlscert",
                        str(self.config.buildkit_cert),
                        "--tlskey",
                        str(self.config.buildkit_key),
                        "build",
                        "--frontend",
                        "dockerfile.v0",
                        "--local",
                        f"context={context}",
                        "--local",
                        f"dockerfile={context}",
                        "--opt",
                        f"platform=linux/{architecture}",
                        "--opt",
                        f"build-arg:BASE_IMAGE={self.config.runtime_base_image}",
                        "--opt",
                        "build-arg:SOURCE_DATE_EPOCH=0",
                        "--output",
                        f"type=docker,name={image_name},dest={image},rewrite-timestamp=true",
                        "--metadata-file",
                        str(metadata),
                    ],
                    cwd=job,
                )
            )
            build_metadata = json.loads(metadata.read_text(encoding="utf-8"))
            image_digest = build_metadata.get("containerimage.digest")
            if not isinstance(image_digest, str) or not _SHA256_RE.fullmatch(image_digest):
                msg = "BuildKit did not return a valid image digest"
                raise RuntimeError(msg)

            scan_inputs = {
                "runtime": (image, image_digest),
                "postgresql": (
                    self.config.support_images / "postgresql.tar",
                    self.config.postgres_image.rpartition("@")[2],
                ),
                "redis": (
                    self.config.support_images / "redis.tar",
                    self.config.redis_image.rpartition("@")[2],
                ),
            }
            sboms: dict[str, dict] = {}
            reports: dict[str, dict] = {}
            critical: list[str] = []
            for name, (archive, digest) in scan_inputs.items():
                logs.append(self._verify_image_archive(job, archive, digest))
                sbom, image_report, scan_logs = self._scan_image(
                    job,
                    name=name,
                    image=archive,
                    image_digest=digest,
                )
                sboms[name] = sbom
                reports[name] = image_report
                logs.extend(scan_logs)
                critical.extend(f"{name}:{finding}" for finding in self._scan_findings(image_report))
            report = {
                "base_image_digest": actual_base_digest,
                "images": reports,
                "critical_findings": sorted(critical),
            }
            self._write_json(job / "trivy.json", report)
            if critical and request.critical_override is None:
                blocked = WorkerBuildStatus(
                    job_id=job_id,
                    status="blocked",
                    logs="\n".join(filter(None, logs)),
                    scan_report=report,
                )
                self._write_json(job / "status.json", blocked.model_dump(mode="json"))
                return
            if request.critical_override is not None:
                report["critical_override"] = request.critical_override
                self._write_json(job / "trivy.json", report)

            package, signature = self._stage_package(
                job,
                request,
                runtime_image=image,
                runtime_image_digest=image_digest,
                sboms=sboms,
                scan_report=report,
            )
            digest = f"sha256:{sha256_file(package)}"

            artifact = WorkerArtifact(
                artifact_type="package",
                location=f"/v1/builds/{job_id}/artifacts/package",
                digest=digest,
                size_bytes=package.stat().st_size,
                checksums={package.name: digest},
                sbom=sboms,
                signature=signature,
            )
            succeeded = WorkerBuildStatus(
                job_id=job_id,
                status="succeeded",
                logs="\n".join(filter(None, logs)),
                scan_report=report,
                artifacts=[artifact],
            )
            self._write_json(job / "status.json", succeeded.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001 - persist the failure for polling clients
            failed = WorkerBuildStatus(job_id=job_id, status="failed", logs=str(exc))
            self._write_json(job / "status.json", failed.model_dump(mode="json"))

    def artifact(self, job_id: str) -> Path:
        worker_status = self._status(job_id)
        if worker_status.status != "succeeded":
            msg = "Build artifact is not ready"
            raise KeyError(msg)
        matches = list(self._job(job_id).glob("unnest-*-rocky9-amd64.tar"))
        if len(matches) != 1 or matches[0].is_symlink():
            msg = "Build package is unavailable"
            raise KeyError(msg)
        return matches[0]

    def push_registry(self, job_id: str, request: RegistryPushRequest) -> WorkerArtifact:
        worker_status = self._status(job_id)
        if worker_status.status != "succeeded":
            msg = "Build artifact is not ready"
            raise KeyError(msg)
        if self.config.registry_credentials is None:
            msg = "Registry credential directory is unavailable"
            raise RuntimeError(msg)
        credential_path = self.config.registry_credentials / request.credential_secret_name
        credential_dir = credential_path.resolve()
        if credential_path.is_symlink() or credential_dir.parent != self.config.registry_credentials:
            msg = "Registry credential path is invalid"
            raise RuntimeError(msg)
        authfile = credential_dir / "config.json"
        if not authfile.is_file() or authfile.is_symlink():
            msg = "Registry credential is unavailable"
            raise RuntimeError(msg)
        job = self._job(job_id)
        digest_file = job / "registry.digest"
        self._run(
            [
                "skopeo",
                "copy",
                "--authfile",
                str(authfile),
                "--digestfile",
                str(digest_file),
                f"docker-archive:{job / 'unnest-runtime.tar'}",
                f"docker://{request.reference}",
            ],
            cwd=job,
        )
        digest = digest_file.read_text(encoding="utf-8").strip()
        if not _SHA256_RE.fullmatch(digest):
            msg = "Registry did not return a valid image digest"
            raise RuntimeError(msg)
        signature = None
        source = worker_status.artifacts[0]
        if source.signature:
            if self.config.cosign_key is None:
                msg = "Cosign key is unavailable"
                raise RuntimeError(msg)
            self._run(
                ["cosign", "sign", "--yes", "--key", str(self.config.cosign_key), f"{request.reference}@{digest}"],
                cwd=job,
                env={**os.environ, "DOCKER_CONFIG": str(credential_dir)},
            )
            signature = f"{request.reference}@{digest}"
        return WorkerArtifact(
            artifact_type="registry",
            location=request.reference,
            digest=digest,
            checksums={request.reference: digest},
            sbom=source.sbom,
            signature=signature,
        )


def create_build_worker_app(worker: DockerImageBuildWorker | None = None) -> FastAPI:
    build_worker = worker or DockerImageBuildWorker(BuildWorkerConfig.from_env())
    app = FastAPI(title="Unnest Build Worker", docs_url=None, redoc_url=None)

    @app.post("/v1/builds", response_model=WorkerBuildStatus, status_code=status.HTTP_202_ACCEPTED)
    async def submit(request: BuildRequest, background: BackgroundTasks) -> WorkerBuildStatus:
        result = build_worker.submit(request)
        if result.status == "queued":
            background.add_task(build_worker.run, result.job_id)
        return result

    @app.get("/v1/builds/{job_id}", response_model=WorkerBuildStatus)
    async def get_build(job_id: str) -> WorkerBuildStatus:
        try:
            return build_worker._status(job_id)  # noqa: SLF001
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get("/v1/builds/{job_id}/artifacts/{artifact_type}")
    async def download(job_id: str, artifact_type: str) -> FileResponse:
        if artifact_type != "package":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        try:
            artifact = build_worker.artifact(job_id)
        except (KeyError, OSError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return FileResponse(artifact, media_type="application/x-tar", filename=artifact.name)

    @app.post("/v1/builds/{job_id}/registry", response_model=WorkerArtifact)
    async def push_registry(job_id: str, request: RegistryPushRequest) -> WorkerArtifact:
        try:
            return build_worker.push_registry(job_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    import ssl

    import uvicorn

    cert = os.getenv("UNNEST_BUILD_WORKER_TLS_CERT")
    key = os.getenv("UNNEST_BUILD_WORKER_TLS_KEY")
    ca = os.getenv("UNNEST_BUILD_WORKER_TLS_CA")
    if not all((cert, key, ca)):
        msg = "Build worker server mTLS configuration is incomplete"
        raise SystemExit(msg)
    uvicorn.run(
        create_build_worker_app(),
        host=os.getenv("UNNEST_BUILD_WORKER_HOST", "0.0.0.0"),  # noqa: S104 - isolated worker must be reachable
        port=int(os.getenv("UNNEST_BUILD_WORKER_PORT", "8443")),
        ssl_certfile=cert,
        ssl_keyfile=key,
        ssl_ca_certs=ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
