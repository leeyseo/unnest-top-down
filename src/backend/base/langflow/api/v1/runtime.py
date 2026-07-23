"""Minimal API surface for an exported on-premise runtime."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import mimetypes
import os
import tempfile
import time
import zipfile
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from struct import pack
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import filetype
import httpx
from anyio import Path as AsyncPath
from fastapi import APIRouter, BackgroundTasks, Body, Depends, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from jsonschema import Draft202012Validator
from lfx.log.logger import logger
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, func, select
from starlette.background import BackgroundTask

from langflow.api.utils import CurrentActiveUser, DbSession, DbSessionReadOnly, build_content_disposition
from langflow.api.v1.endpoints import _run_flow_internal, simple_run_flow
from langflow.api.v1.schemas import ApiKeysResponse, RunResponse, SimplifiedAPIRequest
from langflow.schema.graph import Tweaks
from langflow.services.auth.utils import get_current_active_superuser
from langflow.services.database.models.api_key.crud import create_api_key, delete_api_key, get_api_keys, hash_api_key
from langflow.services.database.models.api_key.model import ApiKey, ApiKeyCreate, UnmaskedApiKeyRead
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow, FlowRead
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.jobs.model import Job, JobStatus, JobType
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.message.model import MessageTable
from langflow.services.database.models.runtime_audit import RuntimeAuditCheckpoint, RuntimeAuditEvent
from langflow.services.database.models.runtime_configuration import RuntimeConfiguration
from langflow.services.database.models.runtime_document import DocumentVersion, IndexGeneration, RuntimeDocument
from langflow.services.database.models.runtime_schedule import RuntimeSchedule
from langflow.services.database.models.user.model import User
from langflow.services.deployment import SandboxWorkerClient
from langflow.services.deps import (
    get_auth_service,
    get_job_service,
    get_queue_service,
    get_settings_service,
    get_storage_service,
    get_task_service,
    session_scope,
)
from langflow.services.runtime_audit import (
    append_runtime_audit_event,
    create_runtime_audit_checkpoint,
    verify_runtime_audit_chain,
)
from langflow.services.runtime_document import (
    DuplicateStrategy,
    activate_document_version,
    activate_index_generation,
    create_shadow_generation,
    fail_document_version,
    fail_index_generation,
    move_document_to_trash,
    register_document,
    restore_document,
)
from langflow.services.runtime_license import runtime_license_status
from langflow.services.runtime_metrics import (
    INGESTION_JOBS,
    LICENSE_VALID,
    QUEUE_VALUES,
    QUOTA_REJECTIONS,
    SETUP_COMPLETE,
)
from langflow.services.runtime_quota import RuntimeQuotaExceededError, get_runtime_quota_service
from langflow.services.runtime_scheduler import next_cron_run
from langflow.services.runtime_setup import (
    encrypt_runtime_secrets,
    generate_age_recovery_key,
    load_or_create_master_key,
    master_key_fingerprint,
)
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


class RuntimeAuditEventRead(BaseModel):
    id: UUID
    sequence: int
    previous_hash: str
    event_hash: str
    event_type: str
    actor_user_id: UUID | None
    resource_type: str | None
    resource_id: str | None
    details: dict[str, Any]
    occurred_at: datetime


class RuntimeScheduleCreate(BaseModel):
    name: str
    cron_expression: str
    timezone: str = "UTC"
    api_version: str = "v1"
    request_payload: dict[str, Any]
    enabled: bool = True


class RuntimeScheduleRead(BaseModel):
    id: UUID
    name: str
    cron_expression: str
    timezone: str
    api_version: str
    request_payload: dict[str, Any]
    enabled: bool
    next_run_at: datetime
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_status: str | None
    last_error: str | None


class RuntimeUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=12)
    role: Literal["admin", "general"] = "general"
    is_active: bool = True


class RuntimeUserUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: SecretStr | None = Field(default=None, min_length=12)
    role: Literal["admin", "general"] | None = None
    is_active: bool | None = None


class RuntimeUserRead(BaseModel):
    id: UUID
    username: str
    role: Literal["admin", "general"]
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class RuntimeSetupRequest(BaseModel):
    admin_username: str = Field(min_length=1, max_length=255)
    admin_password: SecretStr = Field(min_length=12)
    model_endpoint: HttpUrl | None = None
    storage_endpoint: HttpUrl | None = None
    tls_certificate_configured: bool = False
    secret_values: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator("model_endpoint", "storage_endpoint")
    @classmethod
    def endpoint_without_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and (value.username or value.password or value.query or value.fragment):
            msg = "Runtime endpoints must not contain credentials, query parameters, or fragments"
            raise ValueError(msg)
        return value


def _runtime_user_read(user: User) -> RuntimeUserRead:
    return RuntimeUserRead(
        id=user.id,
        username=user.username,
        role="admin" if user.is_superuser else "general",
        is_active=user.is_active,
        created_at=user.create_at,
        last_login_at=user.last_login_at,
    )


async def _ensure_another_admin(session: DbSession, user: User) -> None:
    if not user.is_superuser:
        return
    remaining = (
        await session.exec(
            select(func.count(User.id)).where(
                User.id != user.id,
                User.is_superuser.is_(True),
                User.is_active.is_(True),
            )
        )
    ).one()
    if int(remaining) < 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="At least one active admin is required")


async def _record_runtime_audit_event(**kwargs: Any) -> None:
    try:
        async with session_scope() as audit_session:
            await append_runtime_audit_event(audit_session, **kwargs)
    except Exception:  # noqa: BLE001 - audit failure must be operator-visible without hiding the original result
        await logger.aexception("Runtime audit event write failed")


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
        QUOTA_REJECTIONS.labels(exc.limit).inc()
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


async def _setup_complete(session: DbSessionReadOnly) -> bool:
    if os.getenv("UNNEST_RUNTIME_SETUP_COMPLETE", "").lower() in {"1", "true", "yes", "on"}:
        return True
    configuration = await session.get(RuntimeConfiguration, 1)
    return bool(configuration and configuration.setup_complete)


async def _release_for_api(session: DbSessionReadOnly, api_version: str | None = None) -> DeploymentRelease:
    if not await _setup_complete(session):
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


async def _immutable_subflows(
    session: DbSessionReadOnly,
    release: DeploymentRelease,
) -> dict[str, dict[str, Any]]:
    subflows: dict[str, dict[str, Any]] = {}
    for raw_version_id in release.subflow_version_ids:
        try:
            version_id = UUID(str(raw_version_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Release-pinned Subflow Version is invalid",
            ) from exc
        version = await session.get(FlowVersion, version_id)
        flow = await session.get(Flow, version.flow_id) if version is not None else None
        if version is None or flow is None or not isinstance(version.data, dict):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Release-pinned Subflow Version is unavailable",
            )
        subflows[str(flow.id)] = {
            "id": str(flow.id),
            "name": flow.name,
            "description": version.description or flow.description,
            "updated_at": version.created_at.isoformat() if version.created_at else None,
            "flow_version_id": str(version.id),
            "data": copy.deepcopy(version.data),
        }
    return subflows


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


def _knowledge_component_ids(data: dict[str, Any]) -> list[str]:
    matches = []
    for node in data.get("nodes", []):
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        template = node.get("data", {}).get("node", {}).get("template", {})
        if isinstance(template, dict) and "knowledge_base" in template:
            matches.append(node["id"])
    return matches


def _component_metadata(data: dict[str, Any], component_id: str) -> dict[str, Any]:
    node = next(
        (
            item
            for item in data.get("nodes", [])
            if isinstance(item, dict) and item.get("id") == component_id
        ),
        {},
    )
    field = node.get("data", {}).get("node", {}).get("template", {}).get("metadata_json", {})
    value = field.get("value") if isinstance(field, dict) else None
    try:
        decoded = json.loads(value) if isinstance(value, str) and value else value
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _release_index_fingerprint(release: DeploymentRelease) -> str:
    flows = release.manifest.get("flows", [])
    ingestion = next(
        (entry for entry in flows if isinstance(entry, dict) and entry.get("role") == "ingestion"),
        {},
    )
    payload = {
        "ingestion_digest": ingestion.get("digest"),
        "knowledge_base_alias": release.manifest.get("knowledge_base_alias"),
        "services": release.manifest.get("services", []),
    }
    return f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}"


async def _run_ingestion_target(
    *,
    immutable: FlowRead,
    ingestion_data: dict[str, Any],
    subflows: dict[str, dict[str, Any]],
    release_id: UUID,
    user: User,
    document: RuntimeDocument,
    version: DocumentVersion,
    physical_alias: str,
) -> None:
    storage_service = get_storage_service()
    namespace, storage_name = version.storage_path.split("/", 1)
    component_path = storage_service.resolve_component_path(version.storage_path)
    temporary_directory: tempfile.TemporaryDirectory | None = None
    try:
        if not Path(component_path).is_absolute():
            temporary_directory = tempfile.TemporaryDirectory(prefix="unnest-ingestion-")
            component_path = str(Path(temporary_directory.name) / storage_name)
            await AsyncPath(component_path).write_bytes(await storage_service.get_file(namespace, storage_name))
        tweaks = {
            _deployment_file_input_id(ingestion_data): {
                "file_path": component_path,
                "document_id": str(document.id),
                "checksum": version.checksum,
                "mime_type": version.mime_type,
                "metadata": version.document_metadata,
            }
        }
        for component_id in _knowledge_component_ids(ingestion_data):
            metadata = _component_metadata(ingestion_data, component_id)
            metadata.update(
                {
                    "runtime_document_id": str(document.id),
                    "runtime_document_version_id": str(version.id),
                }
            )
            tweaks.setdefault(component_id, {}).update(
                {
                    "knowledge_base": physical_alias,
                    "metadata_json": json.dumps(metadata, sort_keys=True),
                }
            )
        await simple_run_flow(
            flow=immutable,
            input_request=SimplifiedAPIRequest(output_type="any", tweaks=tweaks),
            api_key_user=user,
            context={
                "deployment_release_id": str(release_id),
                "deployment_subflows": subflows,
            },
        )
    finally:
        if temporary_directory is not None:
            await asyncio.to_thread(temporary_directory.cleanup)


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
    generation_id: UUID | None = None
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

            immutable = FlowRead.model_validate(flow, from_attributes=True).model_copy(
                update={"data": copy.deepcopy(ingestion_version.data)}
            )
            subflows = await _immutable_subflows(session, release)
            fingerprint = _release_index_fingerprint(release)
            generation, reset_generation = await create_shadow_generation(
                session,
                knowledge_base_id=document.knowledge_base_id,
                fingerprint=fingerprint,
            )
            generation_id = generation.id
            active_generation = (
                await session.exec(
                    select(IndexGeneration).where(
                        IndexGeneration.knowledge_base_id == document.knowledge_base_id,
                        IndexGeneration.is_active.is_(True),
                    )
                )
            ).first()
            physical_alias = generation.backend_reference.get("alias")
            if not isinstance(physical_alias, str) or not physical_alias:
                logical_alias = str(release.manifest.get("knowledge_base_alias") or "knowledge")
                physical_alias = f"{logical_alias}--{fingerprint[7:15]}-{uuid4().hex[:8]}"
                generation.backend_reference = {
                    "alias": physical_alias,
                    "logical_alias": logical_alias,
                    "job_id": str(job_id),
                }
                session.add(generation)
                await session.flush()

            targets: list[tuple[RuntimeDocument, DocumentVersion]] = []
            if (
                reset_generation
                and active_generation is not None
                and active_generation.id != generation.id
            ):
                targets = list(
                    (
                        await session.exec(
                            select(RuntimeDocument, DocumentVersion)
                            .join(
                                DocumentVersion,
                                DocumentVersion.document_id == RuntimeDocument.id,
                            )
                            .where(
                                RuntimeDocument.knowledge_base_id == document.knowledge_base_id,
                                RuntimeDocument.id != document.id,
                                RuntimeDocument.status == "active",
                                DocumentVersion.status == "active",
                            )
                            .order_by(col(RuntimeDocument.created_at))
                        )
                    ).all()
                )
            targets.append((document, version))
            ingestion_data = copy.deepcopy(ingestion_version.data)
            activate_generation_after = not generation.is_active

        total = len(targets)
        for index, (target_document, target_version) in enumerate(targets, start=1):
            await job_service.update_job_metadata(
                job_id,
                {
                    "stage": "reindexing" if total > 1 else "ingesting",
                    "progress": 10 + round(80 * (index - 1) / total),
                    "item": index,
                    "items": total,
                    "index_generation_id": str(generation_id),
                },
            )
            await _run_ingestion_target(
                immutable=immutable,
                ingestion_data=ingestion_data,
                subflows=subflows,
                release_id=release_id,
                user=user,
                document=target_document,
                version=target_version,
                physical_alias=physical_alias,
            )
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.status == JobStatus.CANCELLED:
                cancellation_code = "LANGFLOW_USER_CANCELLED"
                raise asyncio.CancelledError(cancellation_code)
            await activate_document_version(session, document_id=document_id, version_id=version_id)
            if activate_generation_after:
                generation = await session.get(IndexGeneration, generation_id)
                if generation is None:
                    msg = "Runtime index generation is unavailable"
                    raise RuntimeError(msg)
                await activate_index_generation(
                    session,
                    generation=generation,
                    backend_reference={
                        "alias": physical_alias,
                        "logical_alias": release.manifest.get("knowledge_base_alias"),
                    },
                )
        await job_service.update_job_metadata(job_id, {"stage": "completed", "progress": 100})
    except asyncio.CancelledError:
        async with session_scope() as session:
            await fail_document_version(session, document_id=document_id, version_id=version_id)
            if generation_id is not None:
                await fail_index_generation(session, generation_id=generation_id)
        raise
    except Exception:
        async with session_scope() as session:
            await fail_document_version(session, document_id=document_id, version_id=version_id)
            if generation_id is not None:
                await fail_index_generation(session, generation_id=generation_id)
        raise


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


async def _apply_active_index_tweaks(
    session: DbSessionReadOnly,
    *,
    release: DeploymentRelease,
    flow: FlowRead,
    input_request: SimplifiedAPIRequest,
) -> None:
    alias = release.manifest.get("knowledge_base_alias")
    if not isinstance(alias, str) or not isinstance(flow.data, dict):
        return
    knowledge_base = (
        await session.exec(
            select(KnowledgeBaseRecord).where(
                KnowledgeBaseRecord.user_id == release.user_id,
                KnowledgeBaseRecord.name == alias,
            )
        )
    ).first()
    if knowledge_base is None:
        return
    generation = (
        await session.exec(
            select(IndexGeneration).where(
                IndexGeneration.knowledge_base_id == knowledge_base.id,
                IndexGeneration.is_active.is_(True),
            )
        )
    ).first()
    physical_alias = generation.backend_reference.get("alias") if generation else None
    if not isinstance(physical_alias, str) or not physical_alias:
        return
    active_version_ids = [
        str(value)
        for value in (
            await session.exec(
                select(DocumentVersion.id)
                .join(RuntimeDocument, RuntimeDocument.id == DocumentVersion.document_id)
                .where(
                    RuntimeDocument.knowledge_base_id == knowledge_base.id,
                    RuntimeDocument.status == "active",
                    DocumentVersion.status == "active",
                )
            )
        ).all()
    ]
    tweaks = input_request.tweaks.root if input_request.tweaks is not None else {}
    for component_id in _knowledge_component_ids(flow.data):
        value = tweaks.setdefault(component_id, {})
        if isinstance(value, dict):
            value.update(
                {
                    "knowledge_base": physical_alias,
                    "metadata_filter": json.dumps(
                        {"runtime_document_version_id": active_version_ids},
                        sort_keys=True,
                    ),
                }
            )
    input_request.tweaks = Tweaks(root=tweaks)


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
    subflows: dict[str, dict[str, Any]],
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
        "context": {
            "deployment_release_id": str(release.id),
            "deployment_subflows": copy.deepcopy(subflows),
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
    subflows: dict[str, dict[str, Any]],
    input_request: SimplifiedAPIRequest,
    current_user: User,
    stream: bool,
):
    try:
        client = SandboxWorkerClient.from_env()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    payload = _sandbox_payload(release, flow, subflows, input_request, current_user)
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
    trigger: str = "api",
):
    started_at = time.monotonic()
    release, flow = await _immutable_agent_flow(session, api_version)
    subflows = await _immutable_subflows(session, release)
    input_request = _contract_input(release, payload)
    await _apply_active_index_tweaks(
        session,
        release=release,
        flow=flow,
        input_request=input_request,
    )
    try:
        if release.manifest.get("sandbox", {}).get("required") is True:
            result = await _run_in_sandbox(
                release=release,
                flow=flow,
                subflows=subflows,
                input_request=input_request,
                current_user=current_user,
                stream=stream,
            )
        else:
            # The route accepts no flow identifier: successful authentication grants
            # execution only of the release-pinned Agent version.
            result = await _run_flow_internal(
                background_tasks=background_tasks,
                flow=flow,
                input_request=input_request,
                stream=stream,
                api_key_user=current_user,
                context={
                    "deployment_release_id": str(release.id),
                    "deployment_subflows": subflows,
                },
                http_request=http_request,
            )
        if stream:
            response = result
        else:
            if not isinstance(result, RunResponse):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Agent returned an invalid response",
                )
            response = _contract_output(release, result)
    except Exception as exc:
        await _record_runtime_audit_event(
            event_type="agent.call",
            actor_user_id=current_user.id,
            resource_type="deployment_release",
            resource_id=str(release.id),
            details={
                "api_version": api_version,
                "latency_ms": round((time.monotonic() - started_at) * 1000, 3),
                "status": "error",
                "stream": stream,
                "trigger": trigger,
                "error_type": type(exc).__name__,
            },
        )
        raise
    await _record_runtime_audit_event(
        event_type="agent.call",
        actor_user_id=current_user.id,
        resource_type="deployment_release",
        resource_id=str(release.id),
        details={
            "api_version": api_version,
            "latency_ms": round((time.monotonic() - started_at) * 1000, 3),
            "status": "stream_started" if stream else "success",
            "stream": stream,
            "trigger": trigger,
        },
    )
    return response


async def execute_scheduled_agent(schedule_id: UUID) -> None:
    async with session_scope() as session:
        schedule = await session.get(RuntimeSchedule, schedule_id)
        if schedule is None or not schedule.enabled:
            return
        release = await _release_for_api(session, schedule.api_version)
        user = await session.get(User, release.user_id)
        if user is None:
            msg = "Runtime schedule owner is unavailable"
            raise RuntimeError(msg)
        background_tasks = BackgroundTasks()
        await _run_agent(
            api_version=schedule.api_version,
            stream=False,
            background_tasks=background_tasks,
            payload=copy.deepcopy(schedule.request_payload),
            current_user=user,
            session=session,
            http_request=Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/internal/runtime-schedule",
                    "headers": [],
                    "query_string": b"",
                }
            ),
            trigger="cron",
        )
        await background_tasks()


async def _latest_runtime_release(session: DbSessionReadOnly) -> DeploymentRelease | None:
    return (
        await session.exec(select(DeploymentRelease).order_by(col(DeploymentRelease.created_at).desc()))
    ).first()


@router.get("/api/v1/setup/status")
async def runtime_setup_status(session: DbSessionReadOnly) -> dict[str, Any]:
    release = await _latest_runtime_release(session)
    configuration = await session.get(RuntimeConfiguration, 1)
    required_secrets = release.manifest.get("secret_names", []) if release else []
    return {
        "complete": await _setup_complete(session),
        "release_version": release.version if release else None,
        "license": runtime_license_status(release.version if release else None),
        "required_secret_names": required_secrets if isinstance(required_secrets, list) else [],
        "configured_secret_names": sorted(
            configuration.settings.get("secret_names", []) if configuration else []
        ),
    }


@router.post("/api/v1/setup", status_code=status.HTTP_201_CREATED)
async def complete_runtime_setup(
    payload: RuntimeSetupRequest,
    session: DbSession,
) -> dict[str, Any]:
    if await session.get(RuntimeConfiguration, 1):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Initial setup is already complete")
    release = await _latest_runtime_release(session)
    if release is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Runtime release is unavailable")
    license_status = runtime_license_status(release.version)
    if not license_status["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Offline license is {license_status['reason']}",
        )
    if release.config.get("tls") == "institution" and not payload.tls_certificate_configured:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Institution TLS certificate must be configured",
        )

    required = release.manifest.get("secret_names", [])
    required_names = {str(name) for name in required} if isinstance(required, list) else set()
    supplied_names = set(payload.secret_values)
    if missing := sorted(required_names - supplied_names):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Required runtime secrets are missing: {', '.join(missing)}",
        )
    if unexpected := sorted(supplied_names - required_names):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Runtime secrets are not declared by the release: {', '.join(unexpected)}",
        )

    username = payload.admin_username.strip()
    if not username:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Admin username is required")
    if (await session.exec(select(User.id).where(User.username == username))).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    try:
        key = load_or_create_master_key()
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Runtime master key volume is unavailable",
        ) from exc
    secrets = {name: value.get_secret_value() for name, value in payload.secret_values.items()}
    recovery_identity, backup_recipient = generate_age_recovery_key()
    admin = User(
        username=username,
        password=get_auth_service().get_password_hash(payload.admin_password.get_secret_value()),
        is_superuser=True,
        is_active=True,
    )
    session.add(admin)
    try:
        await session.flush()
        configuration = RuntimeConfiguration(
            id=1,
            setup_complete=True,
            settings={
                "model_endpoint": str(payload.model_endpoint) if payload.model_endpoint else None,
                "storage_endpoint": str(payload.storage_endpoint) if payload.storage_endpoint else None,
                "tls_certificate_configured": payload.tls_certificate_configured,
                "secret_names": sorted(secrets),
                "backup_recipient": backup_recipient,
            },
            encrypted_secrets=encrypt_runtime_secrets(key, secrets),
            master_key_fingerprint=master_key_fingerprint(key),
            created_by_user_id=admin.id,
        )
        session.add(configuration)
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Initial setup is already complete") from exc
    await append_runtime_audit_event(
        session,
        event_type="runtime.setup.completed",
        actor_user_id=admin.id,
        resource_type="runtime_configuration",
        resource_id="1",
        details={
            "release_version": release.version,
            "secret_names": sorted(secrets),
            "tls_certificate_configured": payload.tls_certificate_configured,
        },
    )
    result = await runtime_setup_status(session)
    result["recovery_identity"] = recovery_identity
    return result


@router.get("/ready")
async def ready(session: DbSessionReadOnly) -> dict[str, str]:
    release = await _release_for_api(session)
    license_status = runtime_license_status(release.version)
    return {
        "status": "ok",
        "release_version": release.version,
        "license": "valid" if license_status["valid"] else str(license_status["reason"]),
    }


@router.get("/metrics")
async def runtime_metrics(session: DbSessionReadOnly) -> Response:
    INGESTION_JOBS.clear()
    rows = (
        await session.exec(
            select(Job.status, func.count(Job.job_id))
            .where(Job.type == JobType.INGESTION)
            .group_by(Job.status)
        )
    ).all()
    for job_status, count in rows:
        INGESTION_JOBS.labels(job_status.value).set(count)

    QUEUE_VALUES.clear()
    for name, value in get_queue_service().metrics_snapshot().items():
        if isinstance(value, int | float):
            QUEUE_VALUES.labels(name).set(value)
    SETUP_COMPLETE.set(int(await _setup_complete(session)))
    LICENSE_VALID.set(int(runtime_license_status()["valid"]))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
        trigger="webhook",
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
    api_key = await create_api_key(session, payload, user_id=admin.id)
    await append_runtime_audit_event(
        session,
        event_type="api_key.created",
        actor_user_id=admin.id,
        resource_type="api_key",
        resource_id=str(api_key.id),
        details={"name": api_key.name},
    )
    return api_key


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
    await append_runtime_audit_event(
        session,
        event_type="api_key.deleted",
        actor_user_id=admin.id,
        resource_type="api_key",
        resource_id=str(api_key_id),
    )


@router.get("/api/v1/admin/users")
async def list_runtime_users(
    session: DbSessionReadOnly,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
) -> list[RuntimeUserRead]:
    users = (await session.exec(select(User).order_by(col(User.username)))).all()
    return [_runtime_user_read(user) for user in users]


@router.post("/api/v1/admin/users", status_code=status.HTTP_201_CREATED)
async def create_runtime_user(
    payload: RuntimeUserCreate,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> RuntimeUserRead:
    username = payload.username.strip()
    if (await session.exec(select(User).where(User.username == username))).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    user = User(
        username=username,
        password=get_auth_service().get_password_hash(payload.password.get_secret_value()),
        is_superuser=payload.role == "admin",
        is_active=payload.is_active,
    )
    session.add(user)
    await session.flush()
    await append_runtime_audit_event(
        session,
        event_type="user.created",
        actor_user_id=admin.id,
        resource_type="user",
        resource_id=str(user.id),
        details={"role": payload.role, "is_active": payload.is_active},
    )
    return _runtime_user_read(user)


@router.patch("/api/v1/admin/users/{user_id}")
async def update_runtime_user(
    user_id: UUID,
    payload: RuntimeUserUpdate,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> RuntimeUserRead:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == admin.id and (
        payload.role == "general" or payload.is_active is False
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An admin cannot demote or disable itself")
    if user.is_superuser and (
        payload.role == "general" or payload.is_active is False
    ):
        await _ensure_another_admin(session, user)
    if payload.username is not None:
        username = payload.username.strip()
        existing = (
            await session.exec(select(User).where(User.username == username, User.id != user.id))
        ).first()
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
        user.username = username
    if payload.password is not None:
        user.password = get_auth_service().get_password_hash(payload.password.get_secret_value())
    if payload.role is not None:
        user.is_superuser = payload.role == "admin"
    if payload.is_active is not None:
        user.is_active = payload.is_active
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await append_runtime_audit_event(
        session,
        event_type="user.updated",
        actor_user_id=admin.id,
        resource_type="user",
        resource_id=str(user.id),
        details={
            "username_changed": payload.username is not None,
            "password_changed": payload.password is not None,
            "role": payload.role,
            "is_active": payload.is_active,
        },
    )
    return _runtime_user_read(user)


@router.delete("/api/v1/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runtime_user(
    user_id: UUID,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An admin cannot delete itself")
    if (await session.exec(select(DeploymentRelease.id).where(DeploymentRelease.user_id == user.id))).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A release owner cannot be deleted")
    await _ensure_another_admin(session, user)
    username = user.username
    await session.delete(user)
    await append_runtime_audit_event(
        session,
        event_type="user.deleted",
        actor_user_id=admin.id,
        resource_type="user",
        resource_id=str(user.id),
        details={"username": username},
    )


@router.get("/api/v1/admin/audit")
async def list_runtime_audit_events(
    session: DbSessionReadOnly,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    events = (
        await session.exec(
            select(RuntimeAuditEvent)
            .order_by(col(RuntimeAuditEvent.sequence).desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "events": [
            RuntimeAuditEventRead.model_validate(event, from_attributes=True).model_dump(mode="json")
            for event in events
        ],
        "integrity": await verify_runtime_audit_chain(session),
    }


@router.post("/api/v1/admin/audit/checkpoints", status_code=status.HTTP_201_CREATED)
async def create_runtime_audit_checkpoint_route(
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> dict[str, Any]:
    await append_runtime_audit_event(
        session,
        event_type="audit.checkpoint.created",
        actor_user_id=admin.id,
        resource_type="runtime_audit",
    )
    try:
        checkpoint = await create_runtime_audit_checkpoint(session)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return RuntimeAuditCheckpoint.model_validate(checkpoint, from_attributes=True).model_dump(mode="json")


@router.get("/api/v1/admin/license")
async def get_runtime_license(
    session: DbSessionReadOnly,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
) -> dict[str, Any]:
    release = await _release_for_api(session)
    return runtime_license_status(release.version)


@router.get("/api/v1/admin/schedules")
async def list_runtime_schedules(
    session: DbSessionReadOnly,
    _admin: Annotated[User, Depends(get_current_active_superuser)],
) -> list[RuntimeScheduleRead]:
    schedules = (await session.exec(select(RuntimeSchedule).order_by(col(RuntimeSchedule.name)))).all()
    return [RuntimeScheduleRead.model_validate(schedule, from_attributes=True) for schedule in schedules]


@router.post("/api/v1/admin/schedules", status_code=status.HTTP_201_CREATED)
async def create_runtime_schedule(
    payload: RuntimeScheduleCreate,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> RuntimeScheduleRead:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Schedule name is required")
    if (await session.exec(select(RuntimeSchedule).where(RuntimeSchedule.name == name))).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Schedule name already exists")
    release = await _release_for_api(session, payload.api_version)
    _contract_input(release, payload.request_payload)
    try:
        next_run_at = next_cron_run(payload.cron_expression, payload.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    schedule = RuntimeSchedule(
        name=name,
        cron_expression=payload.cron_expression,
        timezone=payload.timezone,
        api_version=payload.api_version,
        request_payload=copy.deepcopy(payload.request_payload),
        enabled=payload.enabled,
        next_run_at=next_run_at,
        created_by_user_id=admin.id,
    )
    session.add(schedule)
    await session.flush()
    await append_runtime_audit_event(
        session,
        event_type="schedule.created",
        actor_user_id=admin.id,
        resource_type="runtime_schedule",
        resource_id=str(schedule.id),
        details={"api_version": schedule.api_version, "cron_expression": schedule.cron_expression},
    )
    return RuntimeScheduleRead.model_validate(schedule, from_attributes=True)


@router.patch("/api/v1/admin/schedules/{schedule_id}")
async def set_runtime_schedule_enabled(
    schedule_id: UUID,
    enabled: Annotated[bool, Body(embed=True)],
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> RuntimeScheduleRead:
    schedule = await session.get(RuntimeSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    schedule.enabled = enabled
    schedule.next_run_at = next_cron_run(schedule.cron_expression, schedule.timezone)
    schedule.updated_at = datetime.now(timezone.utc)
    session.add(schedule)
    await append_runtime_audit_event(
        session,
        event_type="schedule.enabled" if enabled else "schedule.disabled",
        actor_user_id=admin.id,
        resource_type="runtime_schedule",
        resource_id=str(schedule.id),
    )
    return RuntimeScheduleRead.model_validate(schedule, from_attributes=True)


@router.delete("/api/v1/admin/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runtime_schedule(
    schedule_id: UUID,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> None:
    schedule = await session.get(RuntimeSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    await session.delete(schedule)
    await append_runtime_audit_event(
        session,
        event_type="schedule.deleted",
        actor_user_id=admin.id,
        resource_type="runtime_schedule",
        resource_id=str(schedule.id),
        details={"name": schedule.name},
    )


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
        await append_runtime_audit_event(
            session,
            event_type="file.upload_skipped",
            actor_user_id=_admin.id,
            resource_type="runtime_document",
            resource_id=str(document.id),
            details={"checksum": version.checksum},
        )
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
    await append_runtime_audit_event(
        session,
        event_type="file.uploaded",
        actor_user_id=_admin.id,
        resource_type="runtime_document",
        resource_id=str(document.id),
        details={"checksum": version.checksum, "job_id": str(job_id), "size_bytes": version.size_bytes},
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
    await _record_runtime_audit_event(
        event_type="file.downloaded",
        actor_user_id=_admin.id,
        resource_type="runtime_document",
        resource_id=str(document.id),
        details={"checksum": version.checksum},
    )
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
    await append_runtime_audit_event(
        session,
        event_type="file.deleted",
        actor_user_id=_admin.id,
        resource_type="runtime_document",
        resource_id=str(document.id),
        details={"retention_days": max(1, retention_days)},
    )


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
    await append_runtime_audit_event(
        session,
        event_type="file.restored",
        actor_user_id=_admin.id,
        resource_type="runtime_document",
        resource_id=str(document.id),
    )
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


@router.post("/api/v1/ingestion/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_runtime_ingestion_job(
    job_id: UUID,
    background_tasks: BackgroundTasks,
    session: DbSession,
    admin: Annotated[User, Depends(get_current_active_superuser)],
) -> RuntimeIngestionJobRead:
    previous = await session.get(Job, job_id)
    if previous is None or previous.asset_type != "runtime_document":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingestion job not found")
    if previous.status not in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ingestion job is not retryable")
    metadata = previous.job_metadata or {}
    try:
        document_id = UUID(str(metadata["document_id"]))
        version_id = UUID(str(metadata["version_id"]))
        release_id = UUID(str(metadata["release_id"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ingestion retry metadata is unavailable",
        ) from exc
    in_flight = (
        await session.exec(
            select(Job.job_id).where(
                Job.asset_id == document_id,
                col(Job.status).in_([JobStatus.QUEUED, JobStatus.IN_PROGRESS]),
            )
        )
    ).first()
    if in_flight:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document ingestion is already running")
    release = await session.get(DeploymentRelease, release_id)
    document = await session.get(RuntimeDocument, document_id)
    version = await session.get(DocumentVersion, version_id)
    ingestion_version = (
        await session.get(FlowVersion, release.ingestion_flow_version_id) if release is not None else None
    )
    if (
        release is None
        or document is None
        or version is None
        or version.document_id != document.id
        or ingestion_version is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Immutable ingestion retry state is unavailable",
        )
    document.status = "pending"
    version.status = "pending"
    session.add(document)
    session.add(version)
    retry_job = Job(
        job_id=uuid4(),
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
            "retry_of": str(previous.job_id),
        },
    )
    session.add(retry_job)
    await session.flush()
    background_tasks.add_task(
        _schedule_runtime_ingestion,
        release_id=release.id,
        document_id=document.id,
        version_id=version.id,
        user_id=release.user_id,
        job_id=retry_job.job_id,
    )
    await append_runtime_audit_event(
        session,
        event_type="ingestion.retried",
        actor_user_id=admin.id,
        resource_type="ingestion_job",
        resource_id=str(retry_job.job_id),
        details={"retry_of": str(previous.job_id), "document_id": str(document.id)},
    )
    return RuntimeIngestionJobRead(
        id=retry_job.job_id,
        status=retry_job.status.value,
        document_id=retry_job.asset_id,
        metadata=retry_job.job_metadata or {},
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
    job.finished_timestamp = datetime.now(timezone.utc)
    job.job_metadata = {**(job.job_metadata or {}), "stage": "cancelled"}
    session.add(job)
    metadata = job.job_metadata or {}
    try:
        await fail_document_version(
            session,
            document_id=UUID(str(metadata["document_id"])),
            version_id=UUID(str(metadata["version_id"])),
        )
        if metadata.get("index_generation_id"):
            await fail_index_generation(
                session,
                generation_id=UUID(str(metadata["index_generation_id"])),
            )
    except (KeyError, ValueError):
        pass
    await session.flush()
    await append_runtime_audit_event(
        session,
        event_type="ingestion.cancelled",
        actor_user_id=_admin.id,
        resource_type="ingestion_job",
        resource_id=str(job.job_id),
        details={"document_id": str(job.asset_id) if job.asset_id else None},
    )
    return RuntimeIngestionJobRead(
        id=job.job_id,
        status=job.status.value,
        document_id=job.asset_id,
        metadata=job.job_metadata or {},
    )
