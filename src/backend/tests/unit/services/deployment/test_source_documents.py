from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from langflow.services.deployment.source_documents import (
    SourceDocumentError,
    _copy_and_digest,
    stage_source_documents,
    verify_bundled_source_documents,
)


def _manifest(document_id, contents: bytes) -> dict:
    return {
        "source_documents": [
            {
                "id": str(document_id),
                "name": "guide.txt",
                "size_bytes": len(contents),
                "digest": f"sha256:{hashlib.sha256(contents).hexdigest()}",
                "mime_type": "text/plain",
                "package_path": f"documents/source/{document_id}/guide.txt",
            }
        ]
    }


def test_source_document_staging_rejects_unexpected_upload_entries(tmp_path):
    document_id = uuid4()
    contents = b"source"
    source = tmp_path / "source"
    source.mkdir()
    (source / str(document_id)).write_bytes(contents)
    (source / "unexpected").mkdir()

    with pytest.raises(SourceDocumentError, match="do not exactly match"):
        stage_source_documents(source, tmp_path / "destination", _manifest(document_id, contents))


def test_bundled_source_document_verification_rejects_extra_directories(tmp_path):
    document_id = uuid4()
    contents = b"source"
    package_path = tmp_path / f"documents/source/{document_id}"
    package_path.mkdir(parents=True)
    (package_path / "guide.txt").write_bytes(contents)
    (tmp_path / "documents/source/unexpected").mkdir()

    with pytest.raises(SourceDocumentError, match="do not exactly match"):
        verify_bundled_source_documents(tmp_path, _manifest(document_id, contents))


async def test_failed_source_document_stream_removes_partial_materialization(tmp_path):
    class Storage:
        async def get_file_stream(self, _namespace, _name, *, chunk_size):
            assert chunk_size > 0
            yield b"partial"
            msg = "provider details that must not escape"
            raise RuntimeError(msg)

    target = tmp_path / "document"
    with pytest.raises(OSError, match="Source document storage read failed"):
        await _copy_and_digest(
            Storage(),  # type: ignore[arg-type]
            namespace="owner",
            storage_name="guide.txt",
            expected_size=100,
            destination=target,
        )

    assert not target.exists()
