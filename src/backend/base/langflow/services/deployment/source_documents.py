# ruff: noqa: EM101, EM102, TRY003
"""Snapshot SI-owned files for an immutable offline release."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import anyio
from sqlmodel import select

from langflow.services.database.models.file.model import File
from langflow.services.deployment.offline_package import sha256_file

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from langflow.services.storage.service import StorageService

MAX_SOURCE_DOCUMENTS = 1000
MAX_SOURCE_DOCUMENT_BYTES = 20 * 1024**3
_CHUNK_SIZE = 1024 * 1024
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class SourceDocumentError(ValueError):
    """Raised when a selected source document is missing, changed, or unsafe."""


@dataclass(frozen=True)
class SourceDocumentSnapshot:
    documents: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]


def validated_source_document_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = manifest.get("source_documents", [])
    if not isinstance(entries, list) or len(entries) > MAX_SOURCE_DOCUMENTS:
        raise SourceDocumentError("Release source document manifest is invalid")
    ids: set[str] = set()
    total_size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceDocumentError("Release source document entry is invalid")
        document_id = entry.get("id")
        name = entry.get("name")
        size = entry.get("size_bytes")
        digest = entry.get("digest")
        mime_type = entry.get("mime_type")
        package_path = entry.get("package_path")
        try:
            normalized_id = str(UUID(str(document_id)))
        except ValueError as exc:
            raise SourceDocumentError("Release source document id is invalid") from exc
        if (
            normalized_id in ids
            or not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
            or "\x00" in name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or not isinstance(mime_type, str)
            or not mime_type
            or package_path != f"documents/source/{normalized_id}/{name}"
        ):
            raise SourceDocumentError(f"Release source document entry is invalid: {document_id}")
        ids.add(normalized_id)
        total_size += size
    if total_size > MAX_SOURCE_DOCUMENT_BYTES:
        raise SourceDocumentError("Release source documents exceed the 20 GiB limit")
    return entries


def stage_source_documents(
    source: Path | None,
    destination: Path,
    manifest: dict[str, Any],
) -> None:
    """Verify worker uploads and copy them into their signed package paths."""
    entries = validated_source_document_manifest(manifest)
    expected = {str(entry["id"]) for entry in entries}
    actual = (
        {path.name for path in source.iterdir()}
        if source is not None and source.is_dir() and not source.is_symlink()
        else set()
    )
    if actual != expected:
        raise SourceDocumentError("Uploaded source documents do not exactly match the release manifest")
    (destination / "documents" / "source").mkdir(parents=True)
    for entry in entries:
        document_id = str(entry["id"])
        uploaded = source / document_id if source is not None else Path()
        if (
            uploaded.is_symlink()
            or not uploaded.is_file()
            or uploaded.stat().st_size != entry["size_bytes"]
            or f"sha256:{sha256_file(uploaded)}" != entry["digest"]
        ):
            raise SourceDocumentError(f"Uploaded source document does not match its snapshot: {document_id}")
        target = destination / str(entry["package_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(uploaded, target)
        if f"sha256:{sha256_file(target)}" != entry["digest"]:
            target.unlink(missing_ok=True)
            raise SourceDocumentError(f"Source document changed while being staged: {document_id}")


def verify_bundled_source_documents(package: Path, manifest: dict[str, Any]) -> None:
    entries = validated_source_document_manifest(manifest)
    expected = {str(entry["package_path"]): entry for entry in entries}
    expected_directories = {
        Path(relative).parent.relative_to("documents/source").as_posix()
        for relative in expected
    }
    source_root = package / "documents" / "source"
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    if source_root.exists():
        if source_root.is_symlink() or not source_root.is_dir():
            raise SourceDocumentError("Bundled source document directory is invalid")
        for path in source_root.rglob("*"):
            if path.is_symlink():
                raise SourceDocumentError("Bundled source documents contain an unexpected symbolic link")
            if path.is_file():
                actual_files.add(path.relative_to(package).as_posix())
            elif path.is_dir():
                actual_directories.add(path.relative_to(source_root).as_posix())
            else:
                raise SourceDocumentError("Bundled source documents contain an unexpected entry")
    if actual_files != set(expected) or actual_directories != expected_directories:
        raise SourceDocumentError("Bundled source documents do not exactly match the release manifest")
    for relative, entry in expected.items():
        path = package / relative
        if (
            path.is_symlink()
            or package.resolve() not in path.resolve().parents
            or path.stat().st_size != entry["size_bytes"]
            or f"sha256:{sha256_file(path)}" != entry["digest"]
        ):
            raise SourceDocumentError(f"Bundled source document does not match its snapshot: {entry['id']}")


def _storage_identity(file: File, user_id: UUID) -> tuple[str, str]:
    namespace, separator, storage_name = file.path.partition("/")
    if (
        not separator
        or namespace != str(user_id)
        or not storage_name
        or "/" in storage_name
        or "\\" in storage_name
        or storage_name in {".", ".."}
        or "\x00" in storage_name
    ):
        raise SourceDocumentError(f"Selected source file has an invalid storage identity: {file.id}")
    return namespace, storage_name


async def _owned_files(
    session: AsyncSession,
    *,
    user_id: UUID,
    file_ids: list[UUID],
) -> tuple[dict[UUID, File], list[str]]:
    if len(file_ids) > MAX_SOURCE_DOCUMENTS:
        return {}, [f"At most {MAX_SOURCE_DOCUMENTS} source documents may be bundled"]
    if len(set(file_ids)) != len(file_ids):
        return {}, ["source_file_ids must not contain duplicates"]
    if not file_ids:
        return {}, []
    files = (
        await session.exec(
            select(File).where(
                File.user_id == user_id,
                File.id.in_(file_ids),
            )
        )
    ).all()
    by_id = {UUID(str(file.id)): file for file in files}
    missing = [str(file_id) for file_id in file_ids if file_id not in by_id]
    errors = (
        [f"Selected source files are missing or not owned by the release author: {', '.join(missing)}"]
        if missing
        else []
    )
    return by_id, errors


async def _copy_and_digest(
    storage: StorageService,
    *,
    namespace: str,
    storage_name: str,
    expected_size: int,
    destination: Path | None = None,
) -> str:
    digest = hashlib.sha256()
    size = 0
    output = await anyio.open_file(destination, "xb") if destination is not None else None
    try:
        try:
            async for chunk in storage.get_file_stream(namespace, storage_name, chunk_size=_CHUNK_SIZE):
                size += len(chunk)
                if size > expected_size or size > MAX_SOURCE_DOCUMENT_BYTES:
                    raise SourceDocumentError(f"Source document size changed while reading: {storage_name}")
                digest.update(chunk)
                if output is not None:
                    await output.write(chunk)
        finally:
            if output is not None:
                await output.aclose()
        if size != expected_size:
            raise SourceDocumentError(f"Source document size changed while reading: {storage_name}")
    except BaseException as exc:
        if destination is not None:
            destination.unlink(missing_ok=True)
        if isinstance(exc, SourceDocumentError | OSError) or not isinstance(exc, Exception):
            raise
        msg = f"Source document storage read failed: {storage_name}"
        raise OSError(msg) from exc
    return f"sha256:{digest.hexdigest()}"


async def snapshot_source_documents(
    session: AsyncSession,
    storage: StorageService,
    *,
    user_id: UUID,
    file_ids: list[UUID],
) -> SourceDocumentSnapshot:
    files, errors = await _owned_files(session, user_id=user_id, file_ids=file_ids)
    if errors:
        return SourceDocumentSnapshot((), tuple(errors))
    documents: list[dict[str, Any]] = []
    total_size = 0
    try:
        for file_id in file_ids:
            file = files[file_id]
            namespace, storage_name = _storage_identity(file, user_id)
            total_size += int(file.size)
            if file.size <= 0 or total_size > MAX_SOURCE_DOCUMENT_BYTES:
                raise SourceDocumentError("Selected source documents exceed the 20 GiB release limit")
            digest = await _copy_and_digest(
                storage,
                namespace=namespace,
                storage_name=storage_name,
                expected_size=int(file.size),
            )
            documents.append(
                {
                    "id": str(file_id),
                    "name": storage_name,
                    "size_bytes": int(file.size),
                    "digest": digest,
                    "mime_type": mimetypes.guess_type(storage_name)[0] or "application/octet-stream",
                    "package_path": f"documents/source/{file_id}/{storage_name}",
                }
            )
    except (OSError, SourceDocumentError, ValueError) as exc:
        return SourceDocumentSnapshot((), (str(exc),))
    return SourceDocumentSnapshot(tuple(documents), ())


async def materialize_source_documents(
    session: AsyncSession,
    storage: StorageService,
    *,
    user_id: UUID,
    manifest: dict[str, Any],
    destination: Path,
) -> list[tuple[str, Path]]:
    entries = manifest.get("source_documents", [])
    if not isinstance(entries, list):
        raise SourceDocumentError("Release source document manifest is invalid")
    try:
        ids = [UUID(str(entry["id"])) for entry in entries if isinstance(entry, dict)]
    except (KeyError, ValueError) as exc:
        raise SourceDocumentError("Release source document manifest is invalid") from exc
    if len(ids) != len(entries):
        raise SourceDocumentError("Release source document manifest is invalid")
    files, errors = await _owned_files(session, user_id=user_id, file_ids=ids)
    if errors:
        raise SourceDocumentError(errors[0])
    destination.mkdir()
    materialized: list[tuple[str, Path]] = []
    for entry, file_id in zip(entries, ids, strict=True):
        file = files[file_id]
        namespace, storage_name = _storage_identity(file, user_id)
        if storage_name != entry.get("name") or int(file.size) != entry.get("size_bytes"):
            raise SourceDocumentError(f"Source document changed after release creation: {file_id}")
        target = destination / str(file_id)
        digest = await _copy_and_digest(
            storage,
            namespace=namespace,
            storage_name=storage_name,
            expected_size=int(file.size),
            destination=target,
        )
        if digest != entry.get("digest"):
            target.unlink(missing_ok=True)
            raise SourceDocumentError(f"Source document checksum changed after release creation: {file_id}")
        materialized.append((str(file_id), target))
    return materialized
