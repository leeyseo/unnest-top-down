"""Deterministic assembly helpers for an offline deployment package."""

from __future__ import annotations

import hashlib
import stat
import tarfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

CHECKSUM_FILE = "checksums.sha256"
CHECKSUM_SIGNATURE_FILE = "signatures/checksums.sig"
_UNSIGNED_FILES = frozenset({CHECKSUM_FILE, CHECKSUM_SIGNATURE_FILE})


class OfflinePackageError(ValueError):
    """Raised when package input cannot be assembled safely."""


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for a regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _entries(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        msg = f"Offline package root is invalid: {root}"
        raise OfflinePackageError(msg)
    entries = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in entries:
        mode = path.lstat().st_mode
        if path.is_symlink() or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            msg = f"Offline package contains an unsupported entry: {path.relative_to(root).as_posix()}"
            raise OfflinePackageError(msg)
    return entries


def write_checksums(root: Path) -> dict[str, str]:
    """Write canonical checksums for every signed package file."""
    checksums = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in _entries(root)
        if path.is_file() and path.relative_to(root).as_posix() not in _UNSIGNED_FILES
    }
    if not checksums:
        msg = "Offline package contains no files to checksum"
        raise OfflinePackageError(msg)
    contents = "".join(f"{digest}  {relative}\n" for relative, digest in checksums.items())
    destination = root / CHECKSUM_FILE
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(contents, encoding="utf-8", newline="\n")
    temporary.replace(destination)
    return checksums


def create_reproducible_tar(root: Path, destination: Path) -> None:
    """Create a stable uncompressed tar without preserving host metadata."""
    entries = _entries(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    with tarfile.open(temporary, "w", format=tarfile.GNU_FORMAT) as archive:
        for path in entries:
            relative = path.relative_to(root).as_posix()
            info = archive.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if path.is_dir() or relative.startswith("bin/") else 0o644
            if path.is_file():
                with path.open("rb") as source:
                    archive.addfile(info, source)
            else:
                archive.addfile(info)
    temporary.replace(destination)
