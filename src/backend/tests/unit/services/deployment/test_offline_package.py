import os
from pathlib import Path

import pytest
from langflow.services.deployment.offline_package import (
    OfflinePackageError,
    create_reproducible_tar,
    write_checksums,
)


def _package(root: Path) -> Path:
    files = {
        "manifest/release.json": b"{}",
        "images/unnest-runtime.tar": b"image",
        "bin/unnestctl-linux-amd64": b"binary",
        "signatures/checksums.sig": b"signature",
    }
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    return root


def test_package_tar_is_reproducible_and_checksums_every_signed_file(tmp_path):
    root = _package(tmp_path / "package")
    checksums = write_checksums(root)
    first = tmp_path / "first.tar"
    create_reproducible_tar(root, first)

    for path in root.rglob("*"):
        os.utime(path, (123456789, 123456789), follow_symlinks=False)
    second = tmp_path / "second.tar"
    create_reproducible_tar(root, second)

    assert set(checksums) == {
        "bin/unnestctl-linux-amd64",
        "images/unnest-runtime.tar",
        "manifest/release.json",
    }
    assert first.read_bytes() == second.read_bytes()


def test_package_rejects_symlinks(tmp_path):
    root = _package(tmp_path / "package")
    (root / "images/link.tar").symlink_to(root / "images/unnest-runtime.tar")

    with pytest.raises(OfflinePackageError, match="unsupported entry"):
        write_checksums(root)
