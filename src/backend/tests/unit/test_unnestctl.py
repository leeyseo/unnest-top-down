import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langflow.services.runtime_backup import RuntimeBackupFile, create_runtime_backup
from langflow.services.runtime_setup import generate_age_recovery_key
from langflow.unnestctl import PackageValidationError, app, preflight, verify_package
from typer.testing import CliRunner


def _write_signed_package(root: Path) -> Path:
    signing_key = Ed25519PrivateKey.generate()
    license_key = Ed25519PrivateKey.generate()
    files = {
        "images/runtime.tar": b"oci image",
        "license/license.json": json.dumps(
            {"expires_at": "2099-01-01T00:00:00Z", "release_versions": ["1.0.0"]}
        ).encode(),
        "keys/cosign.pub": signing_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        "keys/license.pub": license_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    }
    files["license/license.sig"] = base64.b64encode(
        license_key.sign(files["license/license.json"])
    )
    manifest = {
        "provider": "unnest-on-prem",
        "release_version": "1.0.0",
        "build": {"signing_enabled": True},
        "deployment": {
            "architecture": "amd64",
            "orchestrator": "compose",
            "accelerator": "cpu",
            "resources": {"cpu": 1, "memory_bytes": 1, "disk_bytes": 1},
        },
        "external_endpoints": [],
        "package": {
            "required_files": [
                "manifest/release.json",
                "license/license.json",
                "license/license.sig",
                "keys/license.pub",
                "keys/cosign.pub",
                "signatures/release-manifest.sig",
            ],
            "required_globs": ["images/*.tar"],
        },
    }
    files["manifest/release.json"] = json.dumps(manifest, sort_keys=True).encode()
    files["signatures/release-manifest.sig"] = base64.b64encode(
        signing_key.sign(files["manifest/release.json"])
    )
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    checksum = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {relative}\n"
        for relative, content in sorted(files.items())
    ).encode()
    (root / "checksums.sha256").write_bytes(checksum)
    signature_path = root / "signatures" / "checksums.sig"
    signature_path.write_bytes(base64.b64encode(signing_key.sign(checksum)))
    return root


def test_verify_package_checks_checksums_signatures_and_license(tmp_path):
    package = _write_signed_package(tmp_path)

    assert verify_package(package)["release_version"] == "1.0.0"

    (package / "images" / "runtime.tar").write_bytes(b"tampered")
    with pytest.raises(PackageValidationError, match="Checksum mismatch"):
        verify_package(package)


def test_verify_package_rejects_checksum_path_traversal(tmp_path):
    package = _write_signed_package(tmp_path)
    digest = hashlib.sha256(b"outside").hexdigest()
    (package / "checksums.sha256").write_text(f"{digest}  ../outside\n", encoding="utf-8")

    with pytest.raises(PackageValidationError, match="Unsafe package path"):
        verify_package(package)


def test_preflight_rejects_non_linux_host(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path)
    monkeypatch.setattr("langflow.unnestctl.platform.system", lambda: "Darwin")

    with pytest.raises(PackageValidationError, match="Only Linux"):
        preflight(package)


def test_restore_command_requires_stopped_runtime_and_restores_offline_state(tmp_path, monkeypatch):
    monkeypatch.setattr("langflow.unnestctl.platform.system", lambda: "Linux")
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(tmp_path / "backups"))
    source_database = tmp_path / "source.db"
    with sqlite3.connect(source_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('restored')")
    source_key = tmp_path / "source.key"
    source_key.write_bytes(b"restored-key")
    identity, recipient = generate_age_recovery_key()
    backup = create_runtime_backup(
        database_url=f"sqlite:///{source_database}",
        recipient=recipient,
        master_key=source_key,
        files=[
            RuntimeBackupFile(
                archive_path="storage/runtime-documents/file.txt",
                contents=b"restored file",
            )
        ],
        release_version="1.0.0",
    )
    identity_file = tmp_path / "recovery.txt"
    identity_file.write_text(identity)
    identity_file.chmod(0o600)
    target_database = tmp_path / "target.db"
    with sqlite3.connect(target_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('old')")
    storage = tmp_path / "storage"
    target_key = tmp_path / "secrets/master.key"
    runner = CliRunner()
    arguments = [
        "restore",
        str(backup.path),
        "--identity",
        str(identity_file),
        "--database-url",
        f"sqlite:///{target_database}",
        "--storage-dir",
        str(storage),
        "--master-key",
        str(target_key),
        "--license-dir",
        str(tmp_path / "license"),
        "--key-dir",
        str(tmp_path / "keys"),
        "--yes",
    ]

    stopped_required = runner.invoke(app, arguments)
    restored = runner.invoke(app, [*arguments, "--runtime-stopped"])

    assert stopped_required.exit_code == 1
    assert "--runtime-stopped" in str(stopped_required.exception)
    assert restored.exit_code == 0, restored.output
    with sqlite3.connect(target_database) as connection:
        value = connection.execute("SELECT value FROM state").fetchone()
    assert value == ("restored",)
    assert (storage / "runtime-documents/file.txt").read_bytes() == b"restored file"
    assert target_key.read_bytes() == b"restored-key"
