"""Minimal API surface for an exported on-premise runtime."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import mimetypes
import os
import tempfile
import zipfile
from collections.abc import AsyncGenerator
from contextlib import suppress
from http import HTTPStatus
from pathlib import Path
from struct import pack
from typing import Annotated, Any
from uuid import UUID, uuid4

import filetype
import httpx
from anyio import Path as AsyncPath
from fastapi import APIRouter, BackgroundTasks, Body, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from jsonschema import Draft202012Validator
from pydantic import BaseModel
from sqlmodel import col, select
from starlette.background import BackgroundTask

from langflow.api.utils import CurrentActiveUser, DbSession, DbSessionReadOnly, build_content_disposition
from langflow.api.v1.endpoints import _run_flow_internal, simple_run_flow
from langflow.api.v1.schemas import ApiKeysResponse, RunResponse, SimplifiedAPIRequest
from langflow.services.auth.utils import get_current_active_superuser
from langflow.services.database.models.api_key.crud import create_api_key, delete_api_key, get_api_keys, hash_api_key
from langflow.services.database.models.api_key.model import ApiKey, ApiKeyCreate, UnmaskedApiKeyRead
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow, FlowRead
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.jobs.model import Job, JobStatus, JobType
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.message.model import MessageTable
from langflow.services.database.models.runtime_document import DocumentVersion, RuntimeDocument
from langflow.services.database.models.user.model import User
from langflow.services.deployment import SandboxWorkerClient
from langflow.services.deps import (
    get_job_service,
    get_settings_service,
    get_storage_service,
    get_task_service,
    session_scope,
)
from langflow.services.runtime_document import (
    DuplicateStrategy,
    activate_document_version,
    move_document_to_trash,
    register_document,
    restore_document,
)
from langflow.services.runtime_quota import RuntimeQuotaExceededError, get_runtime_quota_service
from langflow.services.storage.service import StorageService

router = APIRouter(tags=["Runtime"])
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_RATIO = 100
ARCHIVE_SUFFIXES = {".zip", ".docx", ".pptx", ".xlsx"}


class RuntimeDocumentRead(BaseModel):
    id: UUID
    name: str
    status: str
    version_id: UUID
    version_number: int
    checksum: str
    mime_type: str
    size_bytes: int
    created: bool = False
    job_id: UUID | None = None


class RuntimeIngestionJobRead(BaseModel):
    id: UUID
    status: str
    document_id: UUID | None
    metadata: dict


async def _runtime_api_user(
    request: Request,
    session: DbSessionReadOnly,
    current_user: CurrentActiveUser,
) -> AsyncGenerator[User, None]:
    raw_key = request.headers.get("x-api-key") or request.query_params.get("x-api-key")
    if not raw_key:
        yield current_user
        return

    api_key = (
        await session.exec(
            select(ApiKey).where(
                ApiKey.api_key_hash == hash_api_key(raw_key),
                ApiKey.user_id == current_user.id,
                ApiKey.is_active.is_(True),
            )
        )
    ).first()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Runtime API key is unavailable")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > api_key.max_request_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="API key request size limit exceeded",
                )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length") from exc
    if len(await request.body()) > api_key.max_request_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="API key request size limit exceeded",
        )

    quota = get_runtime_quota_service()
    try:
        await quota.acquire(api_key)
    except RuntimeQuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"API key {exc.limit} limit exceeded",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    try:
        yield current_user
    finally:
        await quota.release(api_key)


RuntimeApiUser = Annotated[User, Depends(_runtime_api_user)]


def _setup_complete() -> bool:
    # ponytail: replace this process-level gate when encrypted runtime settings land.
    return os.getenv("UNNEST_RUNTIME_SETUP_COMPLETE", "").lower() in {"1", "true", "yes", "on"}


async def _release_for_api(session: DbSessionReadOnly, api_version: str | None = None) -> DeploymentRelease:
    if not _setup_complete():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Initial setup is incomplete")
    statement = select(DeploymentRelease)
    if api_version is not None:
        statement = statement.where(DeploymentRelease.api_version == api_version)
    release = (await session.exec(statement.order_by(col(DeploymentRelease.created_at).desc()))).first()
    if release is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runtime API version not found")
    return release


async def _immutable_agent_flow(
    session: DbSessionReadOnly,
    api_version: str,
) -> tuple[DeploymentRelease, FlowRead]:
    release = await _release_for_api(session, api_version)
    version = (
        await session.exec(select(FlowVersion).where(FlowVersion.id == release.agent_flow_version_id))
    ).first()
    flow = (await session.exec(select(Flow).where(Flow.id == version.flow_id))).first() if version else None
    if version is None or flow is None or not isinstance(version.data, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Immutable Agent Flow Version is unavailable",
        )
    immutable = FlowRead.model_validate(flow, from_attributes=True).model_copy(
        update={"data": copy.deepcopy(version.data)}
    )
    return release, immutable


async def _runtime_knowledge_base(
    session: DbSession | DbSessionReadOnly,
) -> tuple[DeploymentRelease, KnowledgeBaseRecord]:
    release = await _release_for_api(session)
    alias = release.manifest.get("knowledge_base_alias")
    if not isinstance(alias, str) or not alias:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Release Knowledge Base alias is unavailable",
        )
    knowledge_base = (
        await session.exec(
            select(KnowledgeBaseRecord).where(
                KnowledgeBaseRecord.user_id == release.user_id,
                KnowledgeBaseRecord.name == alias,
            )
        )
    ).first()
    if knowledge_base is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Release Knowledge Base is unavailable",
        )
    return release, knowledge_base


def _validate_archive(data: bytes, suffix: str) -> None:
    if suffix not in ARCHIVE_SUFFIXES:
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            compressed = sum(max(member.compress_size, 1) for member in members)
            uncompressed = sum(member.file_size for member in members)
            unsafe = any(
                member.filename.startswith(("/", "\\"))
                or ".." in Path(member.filename).parts
                or Path(member.filename).suffix.lower() == ".zip"
                for member in members
            )
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail="Invalid ZIP-based document") from exc
    if len(members) > MAX_ARCHIVE_MEMBERS or uncompressed / max(compressed, 1) > MAX_ARCHIVE_RATIO or unsafe:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail="Unsafe archive structure")


def _validated_upload(file: UploadFile, data: bytes) -> tuple[str, str]:
    name = file.filename or ""
    if not name or Path(name).name != name or any(separator in name for separator in ("/", "\\")):
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail="Invalid file name")
    suffix = Path(name).suffix.lower()
    expected_mime = mimetypes.guess_type(name)[0]
    detected = filetype.guess(data)
    actual_mime = expected_mime if suffix in ARCHIVE_SUFFIXES else detected.mime if detected else expected_mime
    if not suffix or not expected_mime or not actual_mime:
        raise HTTPException(status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE, detail="Unsupported file type")
    if detected and detected.extension != suffix.removeprefix(".") and suffix not in ARCHIVE_SUFFIXES:
        raise HTTPException(
            status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            detail="File extension does not match its content",
        )
    declared = file.content_type
    if declared and declared != "application/octet-stream" and declared.split(";", 1)[0] != actual_mime:
        raise HTTPException(
            status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            detail="Declared MIME type does not match the file",
        )
    _validate_archive(data, suffix)
    return name, actual_mime


async def _scan_with_clamav(data: bytes) -> None:
    host = os.getenv("UNNEST_CLAMAV_HOST", "clamav")
    writer: asyncio.StreamWriter | None = None
    try:
        port = int(os.getenv("UNNEST_CLAMAV_PORT", "3310"))
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
        writer.write(b"zINSTREAM\0")
        for offset in range(0, len(data), 64 * 1024):
            chunk = data[offset : offset + 64 * 1024]
            writer.write(pack("!I", len(chunk)) + chunk)
        writer.write(pack("!I", 0))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=30)
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ClamAV scan is unavailable",
        ) from exc
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    if b"FOUND" in response:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Malware detected")
    if b"OK" not in response:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ClamAV returned an invalid scan result",
        )


def _document_read(
    document: RuntimeDocument,
    version: DocumentVersion,
    *,
    created: bool = False,
    job_id: UUID | None = None,
) -> RuntimeDocumentRead:
    return RuntimeDocumentRead(
        id=document.id,
        name=document.name,
        status=document.status,
        version_id=version.id,
        version_number=version.version_number,
        checksum=version.checksum,
        mime_type=version.mime_type,
        size_bytes=version.size_bytes,
        created=created,
        job_id=job_id,
    )


async def _document_and_version(
    session: DbSession | DbSessionReadOnly,
    *,
    document_id: UUID,
    knowledge_base_id: UUID,
) -> tuple[RuntimeDocument, DocumentVersion]:
    document = (
        await session.exec(
            select(RuntimeDocument).where(
                RuntimeDocument.id == document_id,
                RuntimeDocument.knowledge_base_id == knowledge_base_id,
            )
        )
    ).first()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    version = (
        await session.exec(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(col(DocumentVersion.version_number).desc())
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document version not found")
    return document, version


def _deployment_file_input_id(data: dict) -> str:
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
        msg = "Ingestion Flow must contain exactly one Deployment File Input"
        raise RuntimeError(msg)
    return matches[0]


async def _execute_runtime_ingestion(
    *,
    release_id: UUID,
    document_id: UUID,
    version_id: UUID,
    user_id: UUID,
    job_id: UUID,
) -> None:
    job_service = get_job_service()
    await job_service.update_job_metadata(job_id, {"stage": "loading", "progress": 10})
    temporary_directory: tempfile.TemporaryDirectory | None = None
    try:
        async with session_scope() as session:
            release = await session.get(DeploymentRelease, release_id)
            document = await session.get(RuntimeDocument, document_id)
            version = await session.get(DocumentVersion, version_id)
            user = await session.get(User, user_id)
            ingestion_version = (
                await session.get(FlowVersion, release.ingestion_flow_version_id) if release is not None else None
            )
            flow = await session.get(Flow, ingestion_version.flow_id) if ingestion_version is not None else None
            if any(value is None for value in (release, document, version, user, ingestion_version, flow)):
                msg = "Runtime ingestion state is unavailable"
                raise RuntimeError(msg)
            if not isinstance(ingestion_version.data, dict):
                msg = "Immutable Ingestion Flow Version is invalid"
                raise TypeError(msg)

            storage_service = get_storage_service()
            namespace, storage_name = version.storage_path.split("/", 1)
            component_path = storage_service.resolve_component_path(version.storage_path)
            if not Path(component_path).is_absolute():
                temporary_directory = tempfile.TemporaryDirectory(prefix="unnest-ingestion-")
                component_path = str(Path(temporary_directory.name) / storage_name)
                await AsyncPath(component_path).write_bytes(await storage_service.get_file(namespace, storage_name))

            input_id = _deployment_file_input_id(ingestion_version.data)
            immutable = FlowRead.model_validate(flow, from_attributes=True).model_copy(
                update={"data": copy.deepcopy(ingestion_version.data)}
            )
            request = SimplifiedAPIRequest(
                output_type="any",
                tweaks={
                    input_id: {
                        "file_path": component_path,
                        "document_id": str(document.id),
                        "checksum": version.checksum,
                        "mime_type": version.mime_type,
                        "metadata": version.document_metadata,
                    }
                },
            )
        await job_service.update_job_metadata(job_id, {"stage": "ingesting", "progress": 50})
        await simple_run_flow(flow=immutable, input_request=request, api_key_user=user)
        async with session_scope() as session:
            await activate_document_version(session, document_id=document_id, version_id=version_id)
        await job_service.update_job_metadata(job_id, {"stage": "completed", "progress": 100})
    except Exception:
        async with session_scope() as session:
            document = await session.get(RuntimeDocument, document_id)
            version = await session.get(DocumentVersion, version_id)
            if document is not None:
                document.status = "failed"
                session.add(document)
            if version is not None:
                version.status = "failed"
                session.add(version)
        raise
    finally:
        if temporary_directory is not None:
            await asyncio.to_thread(temporary_directory.cleanup)


async def _schedule_runtime_ingestion(
    *,
    release_id: UUID,
    document_id: UUID,
    version_id: UUID,
    user_id: UUID,
    job_id: UUID,
) -> None:
    task_id = await get_task_service().fire_and_forget_task(
        get_job_service().execute_with_status,
        job_id,
        _execute_runtime_ingestion,
        release_id=release_id,
        document_id=document_id,
        version_id=version_id,
        user_id=user_id,
        job_id=job_id,
        _task_id=str(job_id),
    )
    await get_job_service().update_job_metadata(job_id, {"task_id": task_id})


def _schema_error(schema: dict[str, Any], value: Any) -> str | None:
    error = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    if error is None:
        return None
    location = "/".join(str(part) for part in error.path) or "<root>"
    return f"{location}: {error.message}"


def _contract_input(release: DeploymentRelease, payload: dict[str, Any]) -> SimplifiedAPIRequest:
    contract = release.manifest.get("api", {})
    schema = contract.get("input_schema")
    mappings = contract.get("input_mapping")
    if not isinstance(schema, dict) or not isinstance(mappings, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Release API input contract is unavailable",
        )
    if error := _schema_error(schema, payload):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Request does not match the release API schema at {error}",
        )

    tweaks: dict[str, dict[str, Any]] = {}
    for field_name, binding in mappings.items():
        if field_name not in payload or not isinstance(binding, dict):
            continue
        component_id = binding.get("component_id")
        component_field = binding.get("component_field")
        if not isinstance(component_id, str) or not isinstance(component_field, str):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Release API input mapping is invalid",
            )
        tweaks.setdefault(component_id, {})[component_field] = payload[field_name]
    return SimplifiedAPIRequest(output_type="any", tweaks=tweaks)


def _mapped_result(result: dict[str, Any], component_id: str, path: str) -> Any:
    for run_output in result.get("outputs") or []:
        for component_output in run_output.get("outputs") or []:
            if component_output.get("component_id") != component_id:
                continue
            value: Any = component_output.get("results")
            for part in path.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise KeyError(path)
                value = value[part]
            return value
    raise KeyError(component_id)


def _contract_output(release: DeploymentRelease, result: RunResponse) -> dict[str, Any]:
    contract = release.manifest.get("api", {})
    schema = contract.get("output_schema")
    mappings = contract.get("output_mapping")
    if not isinstance(schema, dict) or not isinstance(mappings, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Release API output contract is unavailable",
        )

    serialized = result.model_dump(mode="json")
    response: dict[str, Any] = {}
    for field_name, binding in mappings.items():
        if not isinstance(binding, dict):
            continue
        component_id = binding.get("component_id")
        result_path = binding.get("result_path")
        if not isinstance(component_id, str) or not isinstance(result_path, str):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Release API output mapping is invalid",
            )
        try:
            response[field_name] = _mapped_result(serialized, component_id, result_path)
        except KeyError:
            continue
    if error := _schema_error(schema, response):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Agent result does not match the release API schema at {error}",
        )
    return response


def _sandbox_payload(
    release: DeploymentRelease,
    flow: FlowRead,
    input_request: SimplifiedAPIRequest,
    current_user: User,
) -> dict[str, Any]:
    return {
        "execution_boundary": "whole-flow",
        "release_id": str(release.id),
        "flow_version_id": str(release.agent_flow_version_id),
        "user_id": str(current_user.id),
        "flow": {
            "id": str(flow.id),
            "name": flow.name,
            "data": copy.deepcopy(flow.data),
        },
        "request": input_request.model_dump(mode="json"),
        "security": {
            "run_as_non_root": True,
            "read_only_root_filesystem": True,
            "drop_capabilities": ["ALL"],
            "network": "deny-by-default",
            "allowed_endpoints": release.manifest.get("sandbox", {}).get("allowed_endpoints", []),
        },
    }


async def _close_sandbox_stream(response: httpx.Response, client: SandboxWorkerClient) -> None:
    await response.aclose()
    await client.aclose()


async def _run_in_sandbox(
    *,
    release: DeploymentRelease,
    flow: FlowRead,
    input_request: SimplifiedAPIRequest,
    current_user: User,
    stream: bool,
):
    try:
        client = SandboxWorkerClient.from_env()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    payload = _sandbox_payload(release, flow, input_request, current_user)
    try:
        if stream:
            response = await client.stream(payload)
            return StreamingResponse(
                response.aiter_raw(),
                media_type="text/event-stream",
                background=BackgroundTask(_close_sandbox_stream, response, client),
            )
        async with client:
            return RunResponse.model_validate(await client.run(payload))
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        await client.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Sandbox worker execution failed") from exc


async def _run_agent(
    *,
    api_version: str,
    stream: bool,
    background_tasks: BackgroundTasks,
    payload: dict[str, Any],
    current_user: User,
    session: DbSessionReadOnly,
    http_request: Request,
):
    release, flow = await _immutable_agent_flow(session, api_version)
    input_request = _contract_input(release, payload)
    if release.manifest.get("sandbox", {}).get("required") is True:
        result = await _run_in_sandbox(
            release=release,
            flow=flow,
            input_request=input_request,
            current_user=current_user,
            stream=stream,
        )
        if stream:
            return result
        return _contract_output(release, result)
    # The route accepts no flow identifier: successful authentication grants
    # execution only of the release-pinned Agent version.
    result = await _run_flow_internal(
        background_tasks=background_tasks,
        flow=flow,
        input_request=input_request,
        stream=stream,
        api_key_user=current_user,
        context={"deployment_release_id": str(release.id)},
        http_request=http_request,
    )
    if stream:
        return result
    if not isinstance(result, RunResponse):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Agent returned an invalid response")
    return _contract_output(release, result)


@router.get("/ready")
async def ready(session: DbSessionReadOnly) -> dict[str, str]:
    release = await _release_for_api(session)
    return {"status": "ok", "release_version": release.version}


@router.post("/api/{api_version}/agent/run", response_model=None)
async def run_agent(
    api_version: str,
    payload: Annotated[dict[str, Any], Body()],
    background_tasks: BackgroundTasks,
    current_user: RuntimeApiUser,
    session: DbSessionReadOnly,
    http_request: Request,
):
    return await _run_agent(
        api_version=api_version,
        stream=False,
        background_tasks=background_tasks,
        payload=payload,
        current_user=current_user,
        session=session,
        http_request=http_request,
    )


@router.post("/api/{api_version}/agent/stream", response_model=None)
async def stream_agent(
    api_version: str,
    payload: Annotated[dict[str, Any], Body()],
    background_tasks: BackgroundTasks,
    current_user: RuntimeApiUser,
    session: DbSessionReadOnly,
    http_request: Request,
):
    return await _run_agent(
        api_version=api_version,
        stream=True,
        background_tasks=background_tasks,
        payload=payload,
        current_user=current_user,
        session=session,
        http_request=http_request,
    )


@router.post("/api/{api_version}/webhooks/{name}", response_model=None)
async def run_webhook(
    api_version: str,
    name: str,  # noqa: ARG001 - named hooks share the release-pinned Agent
    payload: Annotated[dict[str, Any], Body()],
    background_tasks: BackgroundTasks,
    current_user: RuntimeApiUser,
    session: DbSessionReadOnly,
    http_request: Request,
):
    return await _run_agent(
        api_version=api_version,
        stream=False,
        background_tasks=background_tasks,
        payload=payload,
        current_user=current_user,
        session=session,
        http_request=http_request,
    )


@router.get("/api/{api_version}/sessions")
async def list_sessions(
    api_version: str,
    session: DbSessionReadOnly,
    current_user: RuntimeApiUser,
) -> list[str]:
    _release, flow = await _immutable_agent_flow(session, api_version)
    statement = select(MessageTable.session_id).where(MessageTable.flow_id == flow.id).distinct()
    if not current_user.is_superuser:
        statement = statement.where(
            MessageTable.session_metadata["user_id"].as_string() == str(current_user.id)  # type: ignore[index]
        )
    return list((await session.exec(statement)).all())


@router.get("/api/v1/admin/api-keys")
async def list_runtime_api_keys(
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> ApiKeysResponse:
    api_keys = await get_api_keys(session, admin.id)
    return ApiKeysResponse(total_count=len(api_keys), user_id=admin.id, api_keys=api_keys)


@router.post("/api/v1/admin/api-keys", status_code=status.HTTP_201_CREATED)
async def create_runtime_api_key(
    payload: ApiKeyCreate,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> UnmaskedApiKeyRead:
    return await create_api_key(session, payload, user_id=admin.id)


@router.delete("/api/v1/admin/api-keys/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runtime_api_key(
    api_key_id: UUID,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> None:
    try:
        await delete_api_key(session, api_key_id, admin.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found") from exc


@router.get("/api/v1/files")
async def list_runtime_documents(
    session: DbSessionReadOnly,
    _current_user: CurrentActiveUser,
) -> list[RuntimeDocumentRead]:
    _release, knowledge_base = await _runtime_knowledge_base(session)
    documents = (
        await session.exec(
            select(RuntimeDocument)
            .where(RuntimeDocument.knowledge_base_id == knowledge_base.id)
            .order_by(col(RuntimeDocument.created_at).desc())
        )
    ).all()
    result = []
    for document in documents:
        version = (
            await session.exec(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(col(DocumentVersion.version_number).desc())
            )
        ).first()
        if version is not None:
            result.append(_document_read(document, version))
    return result


@router.post("/api/v1/files", status_code=status.HTTP_201_CREATED)
async def upload_runtime_document(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    session: DbSession,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
    storage_service: Annotated[StorageService, Depends(get_storage_service)],
    duplicate_strategy: Annotated[DuplicateStrategy, Form()] = "skip",
) -> RuntimeDocumentRead:
    max_bytes = get_settings_service().settings.max_file_size_upload * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File size is not allowed")
    data = await file.read()
    if not data or len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File size is not allowed")
    name, mime_type = _validated_upload(file, data)
    release, knowledge_base = await _runtime_knowledge_base(session)
    if release.config.get("features", {}).get("clamav") is True:
        await _scan_with_clamav(data)
    storage_name = f"{uuid4().hex}-{name}"
    storage_namespace = "runtime-documents"
    document, version, created = await register_document(
        session,
        user_id=release.user_id,
        knowledge_base_id=knowledge_base.id,
        name=name,
        checksum=f"sha256:{hashlib.sha256(data).hexdigest()}",
        mime_type=mime_type,
        size_bytes=len(data),
        storage_path=f"{storage_namespace}/{storage_name}",
        duplicate_strategy=duplicate_strategy,
    )
    if created:
        await storage_service.save_file(storage_namespace, storage_name, data)
    if not created:
        return _document_read(document, version, created=False)
    ingestion_version = await session.get(FlowVersion, release.ingestion_flow_version_id)
    if ingestion_version is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Immutable Ingestion Flow Version is unavailable",
        )
    job_id = uuid4()
    session.add(
        Job(
            job_id=job_id,
            flow_id=ingestion_version.flow_id,
            status=JobStatus.QUEUED,
            type=JobType.INGESTION,
            user_id=release.user_id,
            asset_id=document.id,
            asset_type="runtime_document",
            job_metadata={
                "stage": "queued",
                "progress": 0,
                "document_id": str(document.id),
                "version_id": str(version.id),
                "release_id": str(release.id),
            },
        )
    )
    await session.flush()
    background_tasks.add_task(
        _schedule_runtime_ingestion,
        release_id=release.id,
        document_id=document.id,
        version_id=version.id,
        user_id=release.user_id,
        job_id=job_id,
    )
    return _document_read(document, version, created=True, job_id=job_id)


@router.get("/api/v1/files/{document_id}/download")
async def download_runtime_document(
    document_id: UUID,
    session: DbSessionReadOnly,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
    storage_service: Annotated[StorageService, Depends(get_storage_service)],
):
    _release, knowledge_base = await _runtime_knowledge_base(session)
    document, version = await _document_and_version(
        session,
        document_id=document_id,
        knowledge_base_id=knowledge_base.id,
    )
    namespace, storage_name = version.storage_path.split("/", 1)
    data = await storage_service.get_file(namespace, storage_name)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=version.mime_type,
        headers={"Content-Disposition": build_content_disposition(document.name)},
    )


@router.delete("/api/v1/files/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runtime_document(
    document_id: UUID,
    session: DbSession,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
) -> None:
    _release, knowledge_base = await _runtime_knowledge_base(session)
    document, _version = await _document_and_version(
        session,
        document_id=document_id,
        knowledge_base_id=knowledge_base.id,
    )
    retention_days = int(os.getenv("UNNEST_DOCUMENT_RETENTION_DAYS", "30"))
    await move_document_to_trash(session, document=document, retention_days=max(1, retention_days))


@router.post("/api/v1/files/{document_id}/restore")
async def restore_runtime_document(
    document_id: UUID,
    session: DbSession,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
) -> RuntimeDocumentRead:
    _release, knowledge_base = await _runtime_knowledge_base(session)
    document, version = await _document_and_version(
        session,
        document_id=document_id,
        knowledge_base_id=knowledge_base.id,
    )
    await restore_document(session, document=document)
    return _document_read(document, version)


@router.get("/api/v1/ingestion/jobs/{job_id}")
async def get_runtime_ingestion_job(
    job_id: UUID,
    session: DbSessionReadOnly,
    _current_user: CurrentActiveUser,
) -> RuntimeIngestionJobRead:
    job = await session.get(Job, job_id)
    if job is None or job.asset_type != "runtime_document":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingestion job not found")
    return RuntimeIngestionJobRead(
        id=job.job_id,
        status=job.status.value,
        document_id=job.asset_id,
        metadata=job.job_metadata or {},
    )


@router.post("/api/v1/ingestion/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_runtime_ingestion_job(
    job_id: UUID,
    session: DbSession,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
) -> RuntimeIngestionJobRead:
    job = await session.get(Job, job_id)
    if job is None or job.asset_type != "runtime_document":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingestion job not found")
    if job.status not in {JobStatus.QUEUED, JobStatus.IN_PROGRESS}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ingestion job is not cancellable")
    await get_task_service().revoke_task((job.job_metadata or {}).get("task_id", job_id))
    job.status = JobStatus.CANCELLED
    session.add(job)
    await session.flush()
    return RuntimeIngestionJobRead(
        id=job.job_id,
        status=job.status.value,
        document_id=job.asset_id,
        metadata=job.job_metadata or {},
    )
