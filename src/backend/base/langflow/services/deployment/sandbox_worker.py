# ruff: noqa: EM101, EM102, TRY003
"""Whole-flow execution server for the isolated on-prem sandbox container."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import anyio
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from sqlmodel import select
from starlette.background import BackgroundTask

from langflow.api.v1.endpoints import _run_flow_internal
from langflow.api.v1.schemas import RunResponse, SimplifiedAPIRequest
from langflow.services.database.models.flow.model import Flow, FlowRead
from langflow.services.database.models.user.model import User
from langflow.services.database.models.variable.model import Variable
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.deployment.sandbox import (
    MAX_SANDBOX_METADATA_BYTES,
    SANDBOX_ATTACHMENT_PATH,
    SANDBOX_FRAME_HEADER,
    SANDBOX_MAX_ATTACHMENT_BYTES,
)
from langflow.services.deps import get_variable_service, session_scope
from langflow.services.runtime_bundle import _load_bundle
from langflow.services.variable.constants import CREDENTIAL_TYPE

_DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300
_MAX_EXECUTION_TIMEOUT_SECONDS = 3600


class SandboxValidationError(ValueError):
    """Raised when a Runtime request does not match the signed release bundle."""


def _execution_timeout_seconds() -> int:
    raw = os.getenv("UNNEST_SANDBOX_EXECUTION_TIMEOUT_SECONDS", str(_DEFAULT_EXECUTION_TIMEOUT_SECONDS))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("Sandbox execution timeout is invalid") from exc
    if not 1 <= value <= _MAX_EXECUTION_TIMEOUT_SECONDS:
        raise RuntimeError("Sandbox execution timeout is invalid")
    return value


class SandboxSecurityContract(BaseModel):
    model_config = {"extra": "forbid"}

    run_as_non_root: bool
    read_only_root_filesystem: bool
    drop_capabilities: list[str]
    network: str
    allowed_endpoints: list[str]


class SandboxAttachment(BaseModel):
    model_config = {"extra": "forbid"}

    component_id: str = Field(min_length=1, max_length=256)
    checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=SANDBOX_MAX_ATTACHMENT_BYTES)


class SandboxExecutionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    execution_boundary: str
    flow_role: Literal["agent", "ingestion"]
    release_id: UUID
    flow_version_id: UUID
    user_id: UUID
    flow: dict[str, Any]
    context: dict[str, Any]
    request: dict[str, Any]
    security: SandboxSecurityContract
    secrets: dict[str, SecretStr] = Field(default_factory=dict, max_length=256)
    attachment: SandboxAttachment | None = None


@dataclass(frozen=True)
class ValidatedSandboxExecution:
    flow: FlowRead
    request: SimplifiedAPIRequest
    context: dict[str, Any]
    user_id: UUID
    secrets: dict[str, SecretStr]


class _FramedBodyReader:
    def __init__(self, request: Request) -> None:
        self._iterator = request.stream().__aiter__()
        self._buffer = bytearray()

    async def read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            try:
                self._buffer.extend(await anext(self._iterator))
            except StopAsyncIteration as exc:
                raise SandboxValidationError("Sandbox ingestion body is truncated") from exc
        value = bytes(self._buffer[:size])
        del self._buffer[:size]
        return value

    async def remaining(self) -> AsyncIterator[bytes]:
        if self._buffer:
            yield bytes(self._buffer)
            self._buffer.clear()
        async for chunk in self._iterator:
            if chunk:
                yield chunk


async def _framed_execution(request: Request) -> tuple[SandboxExecutionRequest, _FramedBodyReader]:
    if request.headers.get("content-type") != "application/vnd.unnest.sandbox-ingestion":
        raise SandboxValidationError("Sandbox ingestion content type is invalid")
    reader = _FramedBodyReader(request)
    metadata_size = SANDBOX_FRAME_HEADER.unpack(
        await reader.read_exact(SANDBOX_FRAME_HEADER.size)
    )[0]
    if not 0 < metadata_size <= MAX_SANDBOX_METADATA_BYTES:
        raise SandboxValidationError("Sandbox ingestion metadata size is invalid")
    try:
        payload = SandboxExecutionRequest.model_validate_json(
            await reader.read_exact(metadata_size)
        )
    except (TypeError, ValueError) as exc:
        raise SandboxValidationError("Sandbox ingestion metadata is invalid") from exc
    return payload, reader


def _release_bundle_root(root: Path | None = None) -> Path:
    if root is None:
        raw = os.getenv("UNNEST_SANDBOX_RELEASE_BUNDLE")
        if not raw:
            raise SandboxValidationError("Sandbox release bundle is not configured")
        configured = Path(raw)
    else:
        configured = root
    resolved = configured.resolve()
    if not resolved.is_dir() or resolved.is_symlink():
        raise SandboxValidationError("Sandbox release bundle is unavailable")
    return resolved


def _validate_subflows(
    supplied: Any,
    *,
    manifest: dict[str, Any],
    bundled_flows: dict[str, dict],
) -> None:
    if not isinstance(supplied, dict):
        raise SandboxValidationError("Sandbox Subflow closure is invalid")
    entries = [
        entry
        for entry in manifest["flows"]
        if isinstance(entry, dict) and entry.get("role") == "subflow"
    ]
    expected = {str(entry["flow_id"]): entry for entry in entries}
    if set(supplied) != set(expected):
        raise SandboxValidationError("Sandbox Subflow closure does not match the release")
    for flow_id, entry in expected.items():
        value = supplied[flow_id]
        version_id = str(entry["id"])
        if (
            not isinstance(value, dict)
            or value.get("id") != flow_id
            or value.get("flow_version_id") != version_id
            or canonical_digest(value.get("data")) != entry.get("digest")
            or value.get("data") != bundled_flows[version_id]
        ):
            raise SandboxValidationError(f"Sandbox Subflow does not match the release: {flow_id}")


def _deployment_file_input_id(data: dict[str, Any]) -> str:
    matches = []
    for node in data.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_data = node.get("data", {})
        node_info = node_data.get("node", {})
        node_type = node_data.get("type") or node_info.get("name")
        if node_type == "DeploymentFileInput" and isinstance(node.get("id"), str):
            matches.append(node["id"])
    if len(matches) != 1:
        raise SandboxValidationError("Sandbox Ingestion Flow file input is invalid")
    return matches[0]


@lru_cache(maxsize=8)
def _verified_bundle(root: Path) -> tuple[dict, dict[str, dict]]:
    return _load_bundle(root)


def validate_sandbox_execution(
    payload: SandboxExecutionRequest,
    *,
    bundle_root: Path | None = None,
) -> ValidatedSandboxExecution:
    """Bind a request to the immutable Flow and policy in the signed image."""
    try:
        manifest, bundled_flows = _verified_bundle(_release_bundle_root(bundle_root))
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise SandboxValidationError("Sandbox release bundle failed verification") from exc
    sandbox = manifest.get("sandbox", {})
    if not isinstance(sandbox, dict) or sandbox.get("required") is not True:
        raise SandboxValidationError("Release does not permit sandbox execution")
    if sandbox.get("max_attachment_bytes") != SANDBOX_MAX_ATTACHMENT_BYTES:
        raise SandboxValidationError("Release sandbox attachment limit is invalid")
    expected_release_id = uuid5(NAMESPACE_URL, str(manifest.get("release_digest")))
    if payload.release_id != expected_release_id or payload.execution_boundary != "whole-flow":
        raise SandboxValidationError("Sandbox execution boundary does not match the release")

    flow_entries = [
        entry
        for entry in manifest["flows"]
        if isinstance(entry, dict) and entry.get("role") == payload.flow_role
    ]
    if len(flow_entries) != 1:
        raise SandboxValidationError(f"Sandbox release must contain one {payload.flow_role.title()} Flow")
    flow_entry = flow_entries[0]
    if str(payload.flow_version_id) != str(flow_entry.get("id")):
        raise SandboxValidationError("Sandbox Flow Version does not match the release")
    try:
        flow = FlowRead.model_validate(payload.flow)
    except (TypeError, ValueError) as exc:
        raise SandboxValidationError("Sandbox Flow is invalid") from exc
    bundled_flow = bundled_flows[str(flow_entry["id"])]
    if (
        str(flow.id) != str(flow_entry.get("flow_id"))
        or canonical_digest(flow.data) != flow_entry.get("digest")
        or flow.data != bundled_flow
    ):
        raise SandboxValidationError("Sandbox Flow does not match the release")

    if set(payload.context) != {
        "deployment_release_id",
        "deployment_subflows",
        "runtime_session_metadata",
    }:
        raise SandboxValidationError("Sandbox execution context is invalid")
    if payload.context["deployment_release_id"] != str(payload.release_id):
        raise SandboxValidationError("Sandbox release context is invalid")
    metadata = payload.context["runtime_session_metadata"]
    if (
        not isinstance(metadata, dict)
        or metadata.get("user_id") != str(payload.user_id)
        or metadata.get("deployment_release_id") != str(payload.release_id)
    ):
        raise SandboxValidationError("Sandbox session context is invalid")
    _validate_subflows(
        payload.context["deployment_subflows"],
        manifest=manifest,
        bundled_flows=bundled_flows,
    )

    expected_endpoints = sandbox.get("allowed_endpoints", [])
    security = payload.security
    if (
        security.run_as_non_root is not True
        or security.read_only_root_filesystem is not True
        or security.drop_capabilities != ["ALL"]
        or security.network != "deny-by-default"
        or security.allowed_endpoints != expected_endpoints
    ):
        raise SandboxValidationError("Sandbox security policy does not match the release")
    expected_secret_names = manifest.get("secret_names", [])
    if not isinstance(expected_secret_names, list) or set(payload.secrets) != set(expected_secret_names):
        raise SandboxValidationError("Sandbox secret names do not match the release")
    try:
        input_request = SimplifiedAPIRequest.model_validate(payload.request)
    except (TypeError, ValueError) as exc:
        raise SandboxValidationError("Sandbox Flow request is invalid") from exc
    if payload.flow_role == "agent":
        if payload.attachment is not None:
            raise SandboxValidationError("Sandbox Agent Flow cannot receive a file attachment")
    else:
        attachment = payload.attachment
        if attachment is None or attachment.component_id != _deployment_file_input_id(flow.data):
            raise SandboxValidationError("Sandbox Ingestion Flow attachment is invalid")
        tweaks = input_request.tweaks.root if input_request.tweaks is not None else {}
        component_tweaks = tweaks.get(attachment.component_id)
        if not isinstance(component_tweaks, dict) or component_tweaks.get("file_path") != SANDBOX_ATTACHMENT_PATH:
            raise SandboxValidationError("Sandbox Ingestion Flow file path is invalid")
    return ValidatedSandboxExecution(
        flow=flow,
        request=input_request,
        context=payload.context,
        user_id=payload.user_id,
        secrets=payload.secrets,
    )


async def _prepare_local_execution(execution: ValidatedSandboxExecution) -> tuple[FlowRead, User]:
    """Create only the disposable local rows required by the LFX runner."""
    username = f"sandbox-{execution.user_id}"
    flow_name = f"sandbox-{execution.flow.id}"
    async with session_scope() as session:
        user = await session.get(User, execution.user_id)
        if user is None:
            user = User(
                id=execution.user_id,
                username=username,
                password=canonical_digest(str(execution.user_id)),
                is_active=True,
                is_superuser=False,
            )
            session.add(user)
            await session.flush()
        flow = await session.get(Flow, execution.flow.id)
        if flow is None:
            flow = Flow(
                id=execution.flow.id,
                user_id=user.id,
                name=flow_name,
                data=execution.flow.data,
                locked=True,
            )
        else:
            flow.user_id = user.id
            flow.name = flow_name
            flow.data = execution.flow.data
            flow.locked = True
        session.add(flow)

        variables = (
            await session.exec(select(Variable).where(Variable.user_id == user.id))
        ).all()
        by_name = {variable.name: variable for variable in variables}
        variable_service = get_variable_service()
        for variable in variables:
            if variable.name not in execution.secrets:
                await session.delete(variable)
        for name, secret in execution.secrets.items():
            value = secret.get_secret_value()
            existing = by_name.get(name)
            if existing is None:
                await variable_service.create_variable(
                    user_id=user.id,
                    name=name,
                    value=value,
                    type_=CREDENTIAL_TYPE,
                    session=session,
                )
            else:
                if existing.type != CREDENTIAL_TYPE:
                    existing.type = CREDENTIAL_TYPE
                    session.add(existing)
                    await session.flush()
                await variable_service.update_variable(
                    user_id=user.id,
                    name=name,
                    value=value,
                    session=session,
                )
        await session.flush()
    executable = execution.flow.model_copy(
        update={"user_id": execution.user_id, "name": flow_name}
    )
    return executable, User(
        id=execution.user_id,
        username=username,
        password=canonical_digest(f"detached:{execution.user_id}"),
        is_active=True,
        is_superuser=False,
    )


async def execute_sandbox_flow(
    execution: ValidatedSandboxExecution,
    *,
    stream: bool,
    background_tasks: BackgroundTasks,
    request: Request,
) -> RunResponse | StreamingResponse:
    flow, user = await _prepare_local_execution(execution)
    return await _run_flow_internal(
        background_tasks=background_tasks,
        flow=flow,
        input_request=execution.request,
        stream=stream,
        api_key_user=user,
        context=execution.context,
        http_request=request,
    )


async def _timed_execution_stream(body: AsyncIterator[bytes | str]) -> AsyncIterator[bytes | str]:
    with anyio.fail_after(_execution_timeout_seconds()):
        async for chunk in body:
            yield chunk


SandboxExecutor = Callable[
    [ValidatedSandboxExecution, bool, BackgroundTasks, Request],
    Awaitable[RunResponse | StreamingResponse],
]


def create_sandbox_worker_app(
    executor: SandboxExecutor | None = None,
) -> FastAPI:
    """Create the internal-only sandbox worker API."""
    if executor is None:
        from langflow.main import get_lifespan

        lifespan = get_lifespan(runtime_only=True)

        async def production_executor(
            execution: ValidatedSandboxExecution,
            stream: bool,  # noqa: FBT001
            background_tasks: BackgroundTasks,
            request: Request,
        ) -> RunResponse | StreamingResponse:
            return await execute_sandbox_flow(
                execution,
                stream=stream,
                background_tasks=background_tasks,
                request=request,
            )

        executor = production_executor
    else:
        lifespan = None

    app = FastAPI(
        title="Unnest Sandbox Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    async def run_request(
        payload: SandboxExecutionRequest,
        *,
        stream: bool,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> RunResponse | StreamingResponse:
        try:
            if payload.flow_role != "agent":
                raise SandboxValidationError("Sandbox Flow must use its role-specific endpoint")
            execution = validate_sandbox_execution(payload)
            with anyio.fail_after(_execution_timeout_seconds()):
                result = await executor(execution, stream, background_tasks, request)
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Sandbox execution timed out",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Sandbox execution failed",
            ) from exc
        else:
            if isinstance(result, StreamingResponse):
                result.body_iterator = _timed_execution_stream(result.body_iterator)
            return result

    @app.post("/v1/flows/run", response_model=None)
    async def run_flow(
        payload: SandboxExecutionRequest,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> RunResponse | StreamingResponse:
        return await run_request(
            payload,
            stream=False,
            background_tasks=background_tasks,
            request=request,
        )

    @app.post("/v1/flows/stream", response_model=None)
    async def stream_flow(
        payload: SandboxExecutionRequest,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> RunResponse | StreamingResponse:
        return await run_request(
            payload,
            stream=True,
            background_tasks=background_tasks,
            request=request,
        )

    @app.post("/v1/flows/ingestion", response_model=None)
    async def ingest_flow(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> RunResponse | StreamingResponse:
        try:
            payload, body = await _framed_execution(request)
            if payload.flow_role != "ingestion":
                raise SandboxValidationError("Sandbox Flow must use its role-specific endpoint")
            execution = validate_sandbox_execution(payload)
            attachment = payload.attachment
            if attachment is None:
                raise SandboxValidationError("Sandbox Ingestion Flow attachment is missing")
            with tempfile.TemporaryDirectory(prefix="unnest-sandbox-ingestion-") as temporary:
                attachment_path = Path(temporary) / "document"
                digest = hashlib.sha256()
                received = 0
                async with await anyio.open_file(attachment_path, "wb") as destination:
                    async for chunk in body.remaining():
                        received += len(chunk)
                        if received > attachment.size_bytes:
                            raise SandboxValidationError("Sandbox ingestion attachment size does not match")
                        digest.update(chunk)
                        await destination.write(chunk)
                if (
                    received != attachment.size_bytes
                    or f"sha256:{digest.hexdigest()}" != attachment.checksum
                ):
                    raise SandboxValidationError("Sandbox ingestion attachment does not match")
                input_request = execution.request.model_copy(deep=True)
                if input_request.tweaks is None:
                    raise SandboxValidationError("Sandbox Ingestion Flow tweaks are missing")
                component_tweaks = input_request.tweaks.root[attachment.component_id]
                if not isinstance(component_tweaks, dict):
                    raise SandboxValidationError("Sandbox Ingestion Flow tweaks are invalid")
                component_tweaks["file_path"] = str(attachment_path)
                with anyio.fail_after(_execution_timeout_seconds()):
                    return await executor(
                        replace(execution, request=input_request),
                        False,  # noqa: FBT003
                        background_tasks,
                        request,
                    )
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Sandbox ingestion timed out",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Sandbox ingestion failed",
            ) from exc

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _close_forwarded_stream(
    response: httpx.Response,
    client: httpx.AsyncClient,
) -> None:
    await response.aclose()
    await client.aclose()


async def _timed_forwarded_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    with anyio.fail_after(_execution_timeout_seconds()):
        async for chunk in response.aiter_raw():
            yield chunk


def create_sandbox_controller_app(
    *,
    executor_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create the key-holding validator that forwards only verified requests."""
    base_url = executor_url or os.getenv(
        "UNNEST_SANDBOX_EXECUTOR_URL",
        "http://sandbox-executor:8091",
    )
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Sandbox executor URL must be an internal HTTP URL")

    app = FastAPI(
        title="Unnest Sandbox Controller",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def verified_payload(
        payload: SandboxExecutionRequest,
        expected_role: Literal["agent", "ingestion"],
    ) -> dict[str, Any]:
        if payload.flow_role != expected_role:
            raise SandboxValidationError("Sandbox Flow must use its role-specific endpoint")
        execution = validate_sandbox_execution(payload)
        forwarded = payload.model_dump(mode="json")
        forwarded["secrets"] = {
            name: value.get_secret_value()
            for name, value in execution.secrets.items()
        }
        return forwarded

    @app.post("/v1/flows/run")
    async def run_flow(payload: SandboxExecutionRequest) -> RunResponse:
        try:
            forwarded = verified_payload(payload, "agent")
            async with httpx.AsyncClient(
                base_url=base_url,
                transport=transport,
                timeout=httpx.Timeout(30, read=None),
                trust_env=False,
            ) as client:
                with anyio.fail_after(_execution_timeout_seconds()):
                    response = await client.post("/v1/flows/run", json=forwarded)
                response.raise_for_status()
                return RunResponse.model_validate(response.json())
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Sandbox executor timed out",
            ) from exc
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Sandbox executor failed",
            ) from exc

    @app.post("/v1/flows/stream", response_model=None)
    async def stream_flow(payload: SandboxExecutionRequest) -> StreamingResponse:
        client: httpx.AsyncClient | None = None
        try:
            forwarded = verified_payload(payload, "agent")
            client = httpx.AsyncClient(
                base_url=base_url,
                transport=transport,
                timeout=httpx.Timeout(30, read=None),
                trust_env=False,
            )
            request = client.build_request("POST", "/v1/flows/stream", json=forwarded)
            with anyio.fail_after(_execution_timeout_seconds()):
                response = await client.send(request, stream=True)
            response.raise_for_status()
            return StreamingResponse(
                _timed_forwarded_stream(response),
                media_type="text/event-stream",
                background=BackgroundTask(_close_forwarded_stream, response, client),
            )
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except TimeoutError as exc:
            if client is not None:
                await client.aclose()
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Sandbox executor timed out",
            ) from exc
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if client is not None:
                await client.aclose()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Sandbox executor failed",
            ) from exc

    @app.post("/v1/flows/ingestion")
    async def ingest_flow(request: Request) -> RunResponse:
        try:
            payload, body = await _framed_execution(request)
            forwarded = verified_payload(payload, "ingestion")
            metadata = json.dumps(forwarded, separators=(",", ":"), sort_keys=True).encode()

            async def framed_body() -> AsyncIterator[bytes]:
                yield SANDBOX_FRAME_HEADER.pack(len(metadata))
                yield metadata
                async for chunk in body.remaining():
                    yield chunk

            async with httpx.AsyncClient(
                base_url=base_url,
                transport=transport,
                timeout=httpx.Timeout(30, read=None),
                trust_env=False,
            ) as client:
                outbound = client.build_request(
                    "POST",
                    "/v1/flows/ingestion",
                    content=framed_body(),
                    headers={"content-type": "application/vnd.unnest.sandbox-ingestion"},
                )
                with anyio.fail_after(_execution_timeout_seconds()):
                    response = await client.send(outbound)
                response.raise_for_status()
                return RunResponse.model_validate(response.json())
        except SandboxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Sandbox executor timed out",
            ) from exc
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Sandbox executor failed",
            ) from exc

    @app.get("/health")
    async def health() -> dict[str, str]:
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                transport=transport,
                timeout=5,
                trust_env=False,
            ) as client:
                response = await client.get("/health")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sandbox executor is unavailable",
            ) from exc
        return {"status": "ok"}

    return app
