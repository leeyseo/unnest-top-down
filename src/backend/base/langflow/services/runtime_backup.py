"""Encrypted, self-validating backup archives for the isolated runtime."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import make_url

from langflow.services.database.service import get_sqlite_database_file_path
from langflow.services.runtime_setup import decrypt_runtime_backup, encrypt_runtime_backup

BACKUP_FORMAT_VERSION = 1
MAX_BACKUP_ENTRIES = 100_000
MAX_MANIFEST_BYTES = 1024 * 1024
BACKUP_SUFFIX = ".unnest-backup"


class RuntimeBackupError(RuntimeError):
    """Raised when a backup cannot be created or verified safely."""


@dataclass(frozen=True)
class RuntimeBackupFile:
    archive_path: str
    contents: bytes


@dataclass(frozen=True)
class RuntimeBackupResult:
    id: str
    path: Path
    checksum: str
    size_bytes: int
    created_at: datetime


def runtime_backup_directory() -> Path:
    path = Path(os.getenv("UNNEST_BACKUP_DIR", "/opt/unnest/backups"))
    if not path.is_absolute():
        msg = "UNNEST_BACKUP_DIR must be an absolute path"
        raise RuntimeBackupError(msg)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        msg = "Runtime backup directory must not be a symbolic link"
        raise RuntimeBackupError(msg)
    path.chmod(0o700)
    return path


def runtime_backup_path(backup_id: str) -> Path:
    try:
        normalized_id = str(UUID(backup_id))
    except ValueError as exc:
        msg = "Runtime backup identifier is invalid"
        raise RuntimeBackupError(msg) from exc
    return runtime_backup_directory() / f"{normalized_id}{BACKUP_SUFFIX}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_backup_result(path: Path) -> RuntimeBackupResult:
    if not path.is_file() or path.is_symlink() or path.suffix != BACKUP_SUFFIX:
        msg = "Runtime backup is unavailable"
        raise RuntimeBackupError(msg)
    try:
        backup_id = str(UUID(path.name.removesuffix(BACKUP_SUFFIX)))
    except ValueError as exc:
        msg = "Runtime backup identifier is invalid"
        raise RuntimeBackupError(msg) from exc
    stat_result = path.stat()
    return RuntimeBackupResult(
        id=backup_id,
        path=path,
        checksum=_sha256_file(path),
        size_bytes=stat_result.st_size,
        created_at=datetime.fromtimestamp(stat_result.st_mtime, timezone.utc),
    )


def list_runtime_backups() -> list[RuntimeBackupResult]:
    backups = []
    for path in runtime_backup_directory().glob(f"*{BACKUP_SUFFIX}"):
        try:
            backups.append(runtime_backup_result(path))
        except RuntimeBackupError:
            continue
    return sorted(backups, key=lambda item: item.created_at, reverse=True)


def _archive_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or str(path) in {"", "."}:
        msg = f"Unsafe runtime backup entry: {value}"
        raise RuntimeBackupError(msg)
    return str(path)


def _add_bytes(archive: tarfile.TarFile, name: str, contents: bytes) -> dict[str, Any]:
    safe_name = _archive_path(name)
    info = tarfile.TarInfo(safe_name)
    info.size = len(contents)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(contents))
    return {"path": safe_name, "size_bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest()}


def _add_file(archive: tarfile.TarFile, name: str, source: Path) -> dict[str, Any]:
    safe_name = _archive_path(name)
    size = source.stat().st_size
    info = tarfile.TarInfo(safe_name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    with source.open("rb") as source_file:
        archive.addfile(info, source_file)
    return {"path": safe_name, "size_bytes": size, "sha256": _sha256_file(source)}


def _database_snapshot(database_url: str, destination: Path) -> str:
    sqlite_path = get_sqlite_database_file_path(database_url)
    if sqlite_path is not None:
        if not sqlite_path.exists():
            msg = "Runtime SQLite database does not exist"
            raise RuntimeBackupError(msg)
        with sqlite3.connect(sqlite_path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        destination.chmod(0o600)
        return "sqlite3"

    url = make_url(database_url)
    if not url.drivername.startswith(("postgresql", "postgres")):
        msg = f"Unsupported runtime database driver: {url.drivername}"
        raise RuntimeBackupError(msg)
    command_url = url.set(drivername="postgresql", password=None).render_as_string(hide_password=False)
    environment = os.environ.copy()
    if url.password:
        environment["PGPASSWORD"] = url.password
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        msg = "PostgreSQL backup tooling is unavailable"
        raise RuntimeBackupError(msg)
    try:
        completed = subprocess.run(  # noqa: S603
            [
                pg_dump,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(destination),
                command_url,
            ],
            env=environment,
            check=False,
            capture_output=True,
            timeout=int(os.getenv("UNNEST_BACKUP_TIMEOUT_SECONDS", "3600")),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        msg = "PostgreSQL backup tooling is unavailable or timed out"
        raise RuntimeBackupError(msg) from exc
    if completed.returncode:
        msg = "PostgreSQL backup failed"
        raise RuntimeBackupError(msg)
    destination.chmod(0o600)
    return "postgresql-custom"


def create_runtime_backup(
    *,
    database_url: str,
    recipient: str,
    master_key: Path,
    files: list[RuntimeBackupFile],
    release_version: str,
    license_files: list[Path] | None = None,
) -> RuntimeBackupResult:
    if not master_key.is_file() or master_key.is_symlink():
        msg = "Runtime master key is unavailable"
        raise RuntimeBackupError(msg)
    backup_id = str(uuid4())
    created_at = datetime.now(timezone.utc)
    destination = runtime_backup_directory() / f"{backup_id}{BACKUP_SUFFIX}"

    with tempfile.TemporaryDirectory(prefix="unnest-backup-") as temporary:
        temporary_path = Path(temporary)
        database_dump = temporary_path / "database.dump"
        archive_path = temporary_path / "archive.tar"
        database_format = _database_snapshot(database_url, database_dump)
        entries: list[dict[str, Any]] = []
        with tarfile.open(archive_path, mode="w") as archive:
            entries.append(_add_file(archive, "database.dump", database_dump))
            entries.append(_add_bytes(archive, "secrets/master.key", master_key.read_bytes()))
            entries.extend(_add_bytes(archive, item.archive_path, item.contents) for item in files)
            entries.extend(
                _add_bytes(archive, f"license/{license_path.name}", license_path.read_bytes())
                for license_path in license_files or []
                if license_path.is_file() and not license_path.is_symlink()
            )
            manifest = {
                "format": "unnest-runtime-backup",
                "format_version": BACKUP_FORMAT_VERSION,
                "backup_id": backup_id,
                "created_at": created_at.isoformat(),
                "release_version": release_version,
                "database_format": database_format,
                "entries": entries,
            }
            _add_bytes(
                archive,
                "manifest.json",
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            )
        encrypt_runtime_backup(archive_path, destination, recipient)

    result = runtime_backup_result(destination)
    return RuntimeBackupResult(
        id=result.id,
        path=result.path,
        checksum=result.checksum,
        size_bytes=result.size_bytes,
        created_at=created_at,
    )


def verify_runtime_backup(path: Path, identity: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        msg = "Runtime backup is unavailable"
        raise RuntimeBackupError(msg)
    descriptor, temporary_name = tempfile.mkstemp(prefix="unnest-backup-verify-", suffix=".tar")
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()
    try:
        decrypt_runtime_backup(path, temporary_path, identity)
        with tarfile.open(temporary_path, mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > MAX_BACKUP_ENTRIES:
                msg = "Runtime backup contains too many entries"
                raise RuntimeBackupError(msg)
            by_name: dict[str, tarfile.TarInfo] = {}
            for member in members:
                safe_name = _archive_path(member.name)
                if not member.isfile() or safe_name in by_name:
                    msg = "Runtime backup contains an unsafe or duplicate entry"
                    raise RuntimeBackupError(msg)
                by_name[safe_name] = member
            manifest_member = by_name.get("manifest.json")
            if manifest_member is None or manifest_member.size > MAX_MANIFEST_BYTES:
                msg = "Runtime backup manifest is unavailable"
                raise RuntimeBackupError(msg)
            extracted_manifest = archive.extractfile(manifest_member)
            if extracted_manifest is None:
                msg = "Runtime backup manifest is unavailable"
                raise RuntimeBackupError(msg)
            manifest: Any = json.load(extracted_manifest)
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "unnest-runtime-backup"
                or manifest.get("format_version") != BACKUP_FORMAT_VERSION
            ):
                msg = "Runtime backup manifest is invalid"
                raise RuntimeBackupError(msg)
            entries = manifest.get("entries")
            if not isinstance(entries, list):
                msg = "Runtime backup entry manifest is invalid"
                raise RuntimeBackupError(msg)
            for entry in entries:
                if not isinstance(entry, dict):
                    msg = "Runtime backup entry manifest is invalid"
                    raise RuntimeBackupError(msg)
                name = _archive_path(str(entry.get("path", "")))
                member = by_name.get(name)
                extracted = archive.extractfile(member) if member is not None else None
                if extracted is None:
                    msg = f"Runtime backup entry is missing: {name}"
                    raise RuntimeBackupError(msg)
                digest = hashlib.sha256()
                size = 0
                while chunk := extracted.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                if size != entry.get("size_bytes") or digest.hexdigest() != entry.get("sha256"):
                    msg = f"Runtime backup entry integrity check failed: {name}"
                    raise RuntimeBackupError(msg)
            if "database.dump" not in by_name or "secrets/master.key" not in by_name:
                msg = "Runtime backup does not contain the required recovery state"
                raise RuntimeBackupError(msg)
            return manifest
    except (tarfile.TarError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, RuntimeBackupError):
            raise
        msg = "Runtime backup could not be decrypted or verified"
        raise RuntimeBackupError(msg) from exc
    finally:
        temporary_path.unlink(missing_ok=True)
