from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid4, uuid5

from langflow.services.database.models.deployment_release import DeploymentRelease
from langflow.services.database.models.flow_version.model import FlowVersion
from langflow.services.database.models.knowledge_base import KnowledgeBaseRecord
from langflow.services.database.models.runtime_document import DocumentVersion, RuntimeDocument
from langflow.services.deployment.manifest import canonical_digest
from langflow.services.runtime_bundle import load_bundled_runtime_release
from sqlmodel import select


async def test_runtime_imports_bundled_release_once(async_session, monkeypatch, tmp_path):
    agent_id, ingestion_id = uuid4(), uuid4()
    source_id = uuid4()
    source_contents = b"immutable source document"
    source_digest = f"sha256:{hashlib.sha256(source_contents).hexdigest()}"
    agent_data = {"nodes": [{"id": "agent"}], "edges": []}
    ingestion_data = {"nodes": [{"id": "ingestion"}], "edges": []}
    flows = [
        {
            "id": str(agent_id),
            "flow_id": str(uuid4()),
            "version_number": 1,
            "digest": canonical_digest(agent_data),
            "role": "agent",
        },
        {
            "id": str(ingestion_id),
            "flow_id": str(uuid4()),
            "version_number": 1,
            "digest": canonical_digest(ingestion_data),
            "role": "ingestion",
        },
    ]
    manifest = {
        "provider": "unnest-on-prem",
        "release_version": "8.7.6",
        "release_digest": canonical_digest(flows),
        "flows": flows,
        "api": {"version": "v8"},
        "deployment": {"default_language": "ko"},
        "acceptance_tests": [
            {
                "name": "health",
                "required": True,
                "request": {"path": "/health"},
                "expected": {"status": 200},
            }
        ],
        "knowledge_base_alias": "shared",
        "source_documents": [
            {
                "id": str(source_id),
                "name": "guide.txt",
                "size_bytes": len(source_contents),
                "digest": source_digest,
                "mime_type": "text/plain",
                "package_path": f"documents/source/{source_id}/guide.txt",
            }
        ],
    }
    (tmp_path / "manifest").mkdir()
    (tmp_path / "flows").mkdir()
    (tmp_path / f"documents/source/{source_id}").mkdir(parents=True)
    (tmp_path / "manifest/release.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / f"flows/{agent_id}.json").write_text(json.dumps(agent_data), encoding="utf-8")
    (tmp_path / f"flows/{ingestion_id}.json").write_text(json.dumps(ingestion_data), encoding="utf-8")
    (tmp_path / f"documents/source/{source_id}/guide.txt").write_bytes(source_contents)

    class Storage:
        def __init__(self):
            self.files: dict[tuple[str, str], bytes] = {}

        async def save_file(self, namespace, name, data, *, append=False):
            key = (namespace, name)
            self.files[key] = self.files.get(key, b"") + data if append else data

    storage = Storage()

    @asynccontextmanager
    async def test_session_scope():
        yield async_session

    monkeypatch.setenv("UNNEST_RELEASE_BUNDLE", str(tmp_path))
    monkeypatch.setattr("langflow.services.runtime_bundle.session_scope", test_session_scope)
    monkeypatch.setattr("langflow.services.runtime_bundle.get_storage_service", lambda: storage)

    assert await load_bundled_runtime_release() is True
    await async_session.commit()
    assert await load_bundled_runtime_release() is False
    assert (await async_session.exec(select(DeploymentRelease))).one().api_version == "v8"
    assert len((await async_session.exec(select(FlowVersion))).all()) == 2
    assert (await async_session.exec(select(KnowledgeBaseRecord))).one().name == "shared"
    release_id = uuid5(NAMESPACE_URL, manifest["release_digest"])
    document_id = uuid5(release_id, str(source_id))
    document = (await async_session.exec(select(RuntimeDocument))).one()
    version = (await async_session.exec(select(DocumentVersion))).one()
    assert document.id == document_id
    assert document.status == "pending"
    assert version.status == "pending"
    assert version.checksum == source_digest
    assert version.storage_path == f"runtime-documents/{document_id.hex}.source"
    assert storage.files[("runtime-documents", f"{document_id.hex}.source")] == source_contents
