import io
import zipfile

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile
from langflow.api.v1.runtime import (
    _scan_with_clamav,
    _validated_upload,
    retry_runtime_ingestion_job,
    upload_runtime_document,
)
from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.jobs.model import Job, JobStatus
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.user.model import User


def test_runtime_upload_rejects_extension_mismatch_and_archive_bomb():
    disguised_pdf = UploadFile(
        filename="notes.txt",
        file=io.BytesIO(b"%PDF-1.4\n"),
        headers={"content-type": "text/plain"},
    )
    with pytest.raises(HTTPException, match="extension"):
        _validated_upload(disguised_pdf, b"%PDF-1.4\n")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.txt", b"a" * 1_000_000)
    archive_file = UploadFile(
        filename="archive.zip",
        file=io.BytesIO(buffer.getvalue()),
        headers={"content-type": "application/zip"},
    )
    with pytest.raises(HTTPException, match="Unsafe archive"):
        _validated_upload(archive_file, buffer.getvalue())


async def test_runtime_upload_rejects_clamav_detection(monkeypatch):
    class Reader:
        async def read(self, _size):
            return b"stream: Eicar-Test-Signature FOUND\0"

    class Writer:
        payload = bytearray()

        def write(self, data):
            self.payload.extend(data)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    writer = Writer()

    async def open_connection(_host, _port):
        return Reader(), writer

    monkeypatch.setattr("langflow.api.v1.runtime.asyncio.open_connection", open_connection)

    with pytest.raises(HTTPException, match="Malware detected"):
        await _scan_with_clamav(b"test")
    assert writer.payload.startswith(b"zINSTREAM\0")


async def test_runtime_upload_saves_once_and_queues_ingestion(async_session, monkeypatch):
    monkeypatch.setenv("UNNEST_RUNTIME_SETUP_COMPLETE", "true")
    owner = User(
        username="runtime-upload-owner",
        password="unused",  # noqa: S106
        is_active=True,
        is_superuser=True,
    )
    flow = Flow(name="runtime-ingestion", user_id=owner.id, data={"nodes": [], "edges": []})
    version = FlowVersion(
        flow_id=flow.id,
        user_id=owner.id,
        data={"nodes": [], "edges": []},
        version_number=1,
    )
    release = DeploymentRelease(
        user_id=owner.id,
        version="1.0.0",
        agent_flow_version_id=version.id,
        ingestion_flow_version_id=version.id,
        config={},
        manifest={"knowledge_base_alias": "shared"},
    )
    knowledge_base = KnowledgeBaseRecord(name="shared", user_id=owner.id)
    async_session.add_all([owner, flow, version, release, knowledge_base])
    await async_session.flush()

    class Storage:
        saved = []

        async def save_file(self, namespace, name, data):
            self.saved.append((namespace, name, data))

    storage = Storage()
    first = await upload_runtime_document(
        file=UploadFile(
            filename="policy.txt",
            file=io.BytesIO(b"policy"),
            headers={"content-type": "text/plain"},
        ),
        background_tasks=BackgroundTasks(),
        session=async_session,
        _admin=owner,
        storage_service=storage,  # type: ignore[arg-type]
        duplicate_strategy="skip",
    )
    duplicate = await upload_runtime_document(
        file=UploadFile(
            filename="policy-copy.txt",
            file=io.BytesIO(b"policy"),
            headers={"content-type": "text/plain"},
        ),
        background_tasks=BackgroundTasks(),
        session=async_session,
        _admin=owner,
        storage_service=storage,  # type: ignore[arg-type]
        duplicate_strategy="skip",
    )

    assert first.created is True
    assert first.job_id is not None
    assert duplicate.created is False
    assert duplicate.job_id is None
    assert len(storage.saved) == 1

    failed_job = await async_session.get(Job, first.job_id)
    assert failed_job is not None
    failed_job.status = JobStatus.FAILED
    async_session.add(failed_job)
    await async_session.flush()
    retried = await retry_runtime_ingestion_job(
        job_id=failed_job.job_id,
        background_tasks=BackgroundTasks(),
        session=async_session,
        admin=owner,
    )
    assert retried.status == JobStatus.QUEUED.value
    assert retried.metadata["retry_of"] == str(failed_job.job_id)
