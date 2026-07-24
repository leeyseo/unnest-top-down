"""mTLS client for the isolated BuildKit worker control API."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from typing_extensions import Self

_WORKER_URL_ENV = "UNNEST_BUILDKIT_WORKER_URL"
_WORKER_CA_ENV = "UNNEST_BUILDKIT_WORKER_CA"
_WORKER_CERT_ENV = "UNNEST_BUILDKIT_WORKER_CERT"
_WORKER_KEY_ENV = "UNNEST_BUILDKIT_WORKER_KEY"


class WorkerArtifact(BaseModel):
    model_config = {"extra": "forbid"}

    artifact_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    location: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(default=0, ge=0)
    checksums: dict[str, str] = Field(min_length=1)
    sbom: dict[str, Any] = Field(min_length=1)
    signature: str | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def valid_checksums(self) -> WorkerArtifact:
        if any(not value.startswith("sha256:") for value in self.checksums.values()):
            msg = "Worker artifact checksums must be SHA-256 digests"
            raise ValueError(msg)
        return self


class WorkerBuildStatus(BaseModel):
    model_config = {"extra": "forbid"}

    job_id: str = Field(min_length=1)
    status: str = Field(pattern=r"^(queued|running|succeeded|failed|blocked)$")
    logs: str = ""
    scan_report: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[WorkerArtifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def successful_build_has_artifacts(self) -> WorkerBuildStatus:
        if self.status == "succeeded" and not self.artifacts:
            msg = "Successful worker builds must include artifacts"
            raise ValueError(msg)
        if self.status == "succeeded" and not self.scan_report:
            msg = "Successful worker builds must include a security scan report"
            raise ValueError(msg)
        return self


def sanitize_flow_for_build(data: dict[str, Any]) -> dict[str, Any]:
    sanitized = copy.deepcopy(data)
    for node in sanitized.get("nodes", []):
        if not isinstance(node, dict):
            continue
        template = node.get("data", {}).get("node", {}).get("template", {})
        if not isinstance(template, dict):
            continue
        for field in template.values():
            if (
                isinstance(field, dict)
                and field.get("password") is True
                and field.get("load_from_db") is not True
            ):
                field["value"] = None
    return sanitized


class BuildKitWorkerClient:
    def __init__(self, *, base_url: str, ca: Path, cert: Path, key: Path) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            msg = "BuildKit worker URL must be an HTTPS URL without embedded credentials"
            raise ValueError(msg)
        for label, path in (("CA", ca), ("certificate", cert), ("private key", key)):
            if not path.is_file():
                msg = f"BuildKit worker {label} file does not exist: {path}"
                raise ValueError(msg)
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            verify=str(ca),
            cert=(str(cert), str(key)),
            timeout=httpx.Timeout(30, read=3600, write=3600),
        )

    @classmethod
    def from_env(cls) -> BuildKitWorkerClient:
        base_url = os.getenv(_WORKER_URL_ENV)
        ca = os.getenv(_WORKER_CA_ENV)
        cert = os.getenv(_WORKER_CERT_ENV)
        key = os.getenv(_WORKER_KEY_ENV)
        missing = [
            name
            for name, value in (("base_url", base_url), ("ca", ca), ("cert", cert), ("key", key))
            if not value
        ]
        if missing:
            msg = f"BuildKit worker mTLS configuration is incomplete: {', '.join(missing)}"
            raise ValueError(msg)
        return cls(
            base_url=cast("str", base_url),
            ca=Path(cast("str", ca)),
            cert=Path(cast("str", cert)),
            key=Path(cast("str", key)),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()

    async def submit(
        self,
        payload: dict[str, Any],
        source_documents: list[tuple[str, Path]] | None = None,
    ) -> WorkerBuildStatus:
        if source_documents:
            with ExitStack() as stack:
                files = [
                    (
                        "documents",
                        (document_id, stack.enter_context(path.open("rb")), "application/octet-stream"),
                    )
                    for document_id, path in source_documents
                ]
                response = await self._client.post(
                    "/v1/builds",
                    data={"request": json.dumps(payload, separators=(",", ":"), sort_keys=True)},
                    files=files,
                )
        else:
            response = await self._client.post("/v1/builds", json=payload)
        response.raise_for_status()
        return WorkerBuildStatus.model_validate(response.json())

    async def get(self, job_id: str) -> WorkerBuildStatus:
        response = await self._client.get(f"/v1/builds/{job_id}")
        response.raise_for_status()
        return WorkerBuildStatus.model_validate(response.json())

    async def push_registry(
        self,
        job_id: str,
        *,
        reference: str,
        credential_secret_name: str,
    ) -> WorkerArtifact:
        response = await self._client.post(
            f"/v1/builds/{job_id}/registry",
            json={
                "reference": reference,
                "credential_secret_name": credential_secret_name,
            },
        )
        response.raise_for_status()
        return WorkerArtifact.model_validate(response.json())

    async def download_artifact(
        self,
        job_id: str,
        artifact_type: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> tuple[str, int]:
        if max_bytes <= 0:
            msg = "Artifact download size limit must be positive"
            raise ValueError(msg)
        digest = hashlib.sha256()
        size = 0
        async with self._client.stream("GET", f"/v1/builds/{job_id}/artifacts/{artifact_type}") as response:
            response.raise_for_status()
            with destination.open("xb") as output:
                async for chunk in response.aiter_bytes():
                    if size + len(chunk) > max_bytes:
                        msg = "Build artifact exceeds its declared size"
                        raise ValueError(msg)
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        return f"sha256:{digest.hexdigest()}", size
