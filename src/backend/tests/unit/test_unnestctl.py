import base64
import hashlib
import json
import os
import socket
import sqlite3
import stat
import tarfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID
from langflow.services.deployment.offline_package import create_reproducible_tar, write_checksums
from langflow.services.runtime_backup import RuntimeBackupFile, create_runtime_backup
from langflow.services.runtime_setup import generate_age_recovery_key
from langflow.unnestctl import (
    PackageValidationError,
    _tcp_port_available,
    app,
    download_installed_backup,
    inspect_installation,
    preflight,
    run_acceptance,
    verify_package,
)
from typer.testing import CliRunner


def _write_signed_package(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    signing_key = Ed25519PrivateKey.generate()
    license_key = Ed25519PrivateKey.generate()
    release_digest = f"sha256:{'1' * 64}"
    signing_public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signing_der = signing_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signer_fingerprint = hashlib.sha256(signing_der).hexdigest()
    trust_directory = root.parent / "trust" / "releases"
    trust_directory.mkdir(parents=True)
    (trust_directory / f"{signer_fingerprint}.pem").write_bytes(signing_public_key)
    license_public_key = root.parent / "trust" / "vendor-license.pem"
    license_public_key.write_bytes(
        license_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setenv("UNNEST_RELEASE_TRUST_DIR", str(trust_directory))
    monkeypatch.setenv("UNNEST_LICENSE_PUBLIC_KEY", str(license_public_key))
    agent_flow = {"nodes": [{"id": "agent"}], "edges": []}
    ingestion_flow = {"nodes": [{"id": "ingestion"}], "edges": []}
    agent_digest = hashlib.sha256(json.dumps(agent_flow, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    ingestion_digest = hashlib.sha256(
        json.dumps(ingestion_flow, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    source_id = "550e8400-e29b-41d4-a716-446655440000"
    source_contents = b"bundled source document"
    files = {
        "compose/compose.yml": b"services: {}\n",
        f"documents/source/{source_id}/guide.txt": source_contents,
        "flows/agent-version.json": json.dumps(agent_flow, sort_keys=True).encode(),
        "flows/ingestion-version.json": json.dumps(ingestion_flow, sort_keys=True).encode(),
        "images/unnest-runtime.tar": b"runtime image",
        "images/postgresql.tar": b"postgresql image",
        "images/redis.tar": b"redis image",
        "license/license.json": json.dumps(
            {"expires_at": "2099-01-01T00:00:00Z", "release_digest": release_digest}
        ).encode(),
        "openapi/openapi.json": b'{"openapi":"3.1.0"}',
        "reports/sbom.cdx.json": b'{"bomFormat":"CycloneDX"}',
        "reports/trivy.json": b'{"critical_findings":[]}',
        "wheels/requirements.lock": b"",
        "tests/acceptance.json": json.dumps(
            [
                {
                    "name": "health",
                    "required": True,
                    "request": {"path": "/health"},
                    "expected": {"status": 200},
                },
                {
                    "name": "agent-smoke",
                    "required": True,
                    "request": {"path": "/api/v1/agent/run", "body": {"message": "hello"}},
                    "expected": {"status": 200, "body": {"answer": "hello"}},
                },
            ],
            sort_keys=True,
        ).encode(),
    }
    files["license/license.sig"] = base64.b64encode(license_key.sign(files["license/license.json"]))
    manifest = {
        "provider": "unnest-on-prem",
        "release_version": "1.0.0",
        "release_digest": release_digest,
        "build": {
            "signing_enabled": True,
            "signer_fingerprint": f"sha256:{signer_fingerprint}",
            "dependency_lock_status": "resolved",
            "resolved_wheels": [],
        },
        "deployment": {
            "architecture": "amd64",
            "orchestrator": "compose",
            "topology": "single",
            "accelerator": "cpu",
            "database": "embedded-postgresql",
            "storage": "local",
            "infrastructure": "bundled",
            "model_runtime": "external",
            "resources": {"cpu": 1, "memory_bytes": 1, "disk_bytes": 1},
        },
        "flows": [
            {
                "id": "agent-version",
                "role": "agent",
                "digest": f"sha256:{agent_digest}",
            },
            {
                "id": "ingestion-version",
                "role": "ingestion",
                "digest": f"sha256:{ingestion_digest}",
            },
        ],
        "images": [
            {
                "archive": relative,
                "archive_digest": f"sha256:{hashlib.sha256(files[relative]).hexdigest()}",
                "image_digest": f"sha256:{str(index) * 64}",
                "reference": f"image-{index}@sha256:{str(index) * 64}",
            }
            for index, relative in enumerate(
                ("images/unnest-runtime.tar", "images/postgresql.tar", "images/redis.tar"),
                start=1,
            )
        ],
        "sandbox": {"required": False},
        "source_documents": [
            {
                "id": source_id,
                "name": "guide.txt",
                "size_bytes": len(source_contents),
                "digest": f"sha256:{hashlib.sha256(source_contents).hexdigest()}",
                "mime_type": "text/plain",
                "package_path": f"documents/source/{source_id}/guide.txt",
            }
        ],
        "external_endpoints": [],
        "dependency_lock": {"python_packages": [], "os_packages": [], "binaries": []},
        "acceptance_tests": json.loads(files["tests/acceptance.json"]),
        "package": {
            "layout_version": 2,
            "required_files": [
                "manifest/release.json",
                "license/license.json",
                "license/license.sig",
                "tests/acceptance.json",
            ],
            "required_globs": ["flows/*.json", "images/*.tar"],
        },
    }
    files["manifest/release.json"] = json.dumps(manifest, sort_keys=True).encode()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    checksum = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {relative}\n" for relative, content in sorted(files.items())
    ).encode()
    (root / "checksums.sha256").write_bytes(checksum)
    signature_path = root / "signatures" / "checksums.sig"
    signature_path.parent.mkdir(parents=True, exist_ok=True)
    signature_path.write_bytes(base64.b64encode(signing_key.sign(checksum)))
    return root


def _write_installed_release(
    root: Path,
    package: Path,
    *,
    services: list[str],
) -> Path:
    release = root / "releases" / "1.0.0"
    (release / "manifest").mkdir(parents=True)
    manifest = json.loads((package / "manifest/release.json").read_text())
    manifest["services"] = services
    (release / "manifest/release.json").write_text(json.dumps(manifest), encoding="utf-8")
    (release / "tls").mkdir()
    (release / "tls/server.crt").write_text("test certificate", encoding="utf-8")
    (release / ".env").write_text(
        f"UNNEST_DB_PASSWORD=test\nUNNEST_INSTALL_GID={os.getegid()}\n",
        encoding="utf-8",
    )
    (release / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "current.json").write_text(
        json.dumps(
            {
                "release_version": manifest["release_version"],
                "release_digest": manifest["release_digest"],
                "directory": str(release),
                "url": "https://127.0.0.1:7860/setup",
            }
        ),
        encoding="utf-8",
    )
    return release


def test_verify_package_checks_checksums_signatures_and_license(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)

    assert verify_package(package)["release_version"] == "1.0.0"

    (package / "images" / "unnest-runtime.tar").write_bytes(b"tampered")
    with pytest.raises(PackageValidationError, match="Checksum mismatch"):
        verify_package(package)


def test_verify_package_accepts_reproducible_tar_archive(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    write_checksums(package)
    archive = tmp_path / "release.tar"
    create_reproducible_tar(package, archive)

    assert verify_package(archive)["release_version"] == "1.0.0"


def test_verify_package_rejects_modified_checksum_manifest_before_parsing(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    digest = hashlib.sha256(b"outside").hexdigest()
    (package / "checksums.sha256").write_text(f"{digest}  ../outside\n", encoding="utf-8")

    with pytest.raises(PackageValidationError, match="not signed by an enrolled SI release key"):
        verify_package(package)


def test_verify_package_rejects_unchecksummed_additional_file(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    (package / "unexpected.txt").write_text("not signed", encoding="utf-8")

    with pytest.raises(PackageValidationError, match="file set does not match"):
        verify_package(package)


def test_verify_package_never_trusts_a_key_supplied_by_the_package(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    enrolled_key = next((tmp_path / "trust" / "releases").glob("*.pem"))
    package_key = package / "keys" / "cosign.pub"
    package_key.parent.mkdir()
    package_key.write_bytes(enrolled_key.read_bytes())
    empty_trust = tmp_path / "government-trust"
    empty_trust.mkdir()
    monkeypatch.setenv("UNNEST_RELEASE_TRUST_DIR", str(empty_trust))

    with pytest.raises(PackageValidationError, match="No trusted SI release key"):
        verify_package(package)


def test_verify_package_rejects_unsafe_archive_names(tmp_path):
    archive = tmp_path / "release.tar"
    with tarfile.open(archive, "w") as release:
        info = tarfile.TarInfo("images\\runtime.tar")
        info.size = 1
        release.addfile(info, BytesIO(b"x"))

    with pytest.raises(PackageValidationError, match="Unsafe release archive path"):
        verify_package(archive)


def test_trust_import_enrolls_key_by_spki_fingerprint(tmp_path):
    public_key = tmp_path / "si-release.pub"
    public_key.write_bytes(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    trust_directory = tmp_path / "trust"

    result = CliRunner().invoke(
        app,
        ["trust", "import", str(public_key), "--trust-dir", str(trust_directory)],
    )

    assert result.exit_code == 0, result.output
    assert len(list(trust_directory.glob("*.pem"))) == 1
    assert "trusted SI release key sha256:" in result.output


def test_preflight_rejects_non_linux_host(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    monkeypatch.setattr("langflow.unnestctl.platform.system", lambda: "Darwin")

    with pytest.raises(PackageValidationError, match="Only Linux"):
        preflight(package)


def test_preflight_port_probe_detects_a_bound_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        assert _tcp_port_available(port, "127.0.0.1") is False

    assert _tcp_port_available(port, "127.0.0.1") is True


def test_install_loads_verified_images_and_starts_persistent_compose(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    commands: list[tuple[list[str], Path]] = []
    monkeypatch.setattr("langflow.unnestctl.platform.system", lambda: "Linux")
    monkeypatch.setattr("langflow.unnestctl._rocky_linux_9", lambda: True)
    monkeypatch.setattr("langflow.unnestctl._docker_compose_available", lambda: True)
    monkeypatch.setattr("langflow.unnestctl.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        "langflow.unnestctl._run",
        lambda command, cwd: commands.append((command, cwd)),
    )
    install_root = tmp_path / "installed"

    result = CliRunner().invoke(
        app,
        [
            "install",
            str(package),
            "--install-root",
            str(install_root),
            "--server-name",
            "127.0.0.1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [command[:3] for command, _cwd in commands[:3]] == [["docker", "image", "load"]] * 3
    assert commands[-1][0][-2:] == ["--pull", "never"]
    release_directory = install_root / "releases" / "1.0.0"
    assert (release_directory / "compose.yml").is_file()
    assert stat.S_IMODE((release_directory / ".env").stat().st_mode) == 0o600
    assert f"UNNEST_INSTALL_GID={os.getegid()}" in (release_directory / ".env").read_text()
    assert x509.load_pem_x509_certificate((release_directory / "tls" / "server.crt").read_bytes())
    runtime_tls = release_directory / "sandbox-tls" / "runtime"
    worker_tls = release_directory / "sandbox-tls" / "worker"
    client_certificate = x509.load_pem_x509_certificate((runtime_tls / "client.crt").read_bytes())
    server_certificate = x509.load_pem_x509_certificate((worker_tls / "server.crt").read_bytes())
    assert (
        ExtendedKeyUsageOID.CLIENT_AUTH
        in client_certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    )
    assert (
        ExtendedKeyUsageOID.SERVER_AUTH
        in server_certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    )
    assert (runtime_tls / "client.key").read_bytes() != (worker_tls / "server.key").read_bytes()
    assert stat.S_IMODE((runtime_tls / "client.key").stat().st_mode) == 0o640
    assert stat.S_IMODE((worker_tls / "server.key").stat().st_mode) == 0o640
    assert json.loads((install_root / "current.json").read_text())["url"] == "https://127.0.0.1:7860/setup"


def test_status_reports_compose_health_and_pre_setup_readiness(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    install_root = tmp_path / "installed"
    services = ["runtime", "postgresql", "redis", "sandbox-controller"]
    release = _write_installed_release(install_root, package, services=services)
    commands = []
    monkeypatch.setattr(
        "langflow.unnestctl._run_capture",
        lambda command, cwd: commands.append((command, cwd)) or "\n".join(services),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.path == "/health" else 503)

    result = inspect_installation(
        install_root,
        transport=httpx.MockTransport(handler),
    )

    assert result["healthy"] is True
    assert result["ready"] is False
    assert result["missing_services"] == []
    assert result["unexpected_services"] == []
    assert commands[0][1] == release
    assert commands[0][0][-4:] == ["ps", "--services", "--status", "running"]


def test_backup_logs_in_downloads_and_verifies_encrypted_archive(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    install_root = tmp_path / "installed"
    _write_installed_release(install_root, package, services=["runtime"])
    backup_id = str(uuid4())
    contents = b"encrypted-runtime-backup"
    checksum = hashlib.sha256(contents).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/login":
            assert b"username=runtime-admin" in request.content
            assert b"password=strong-password" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "token_type": "bearer",
                },
            )
        assert request.headers["authorization"] == "Bearer access-token"
        if request.url.path == "/api/v1/admin/backups":
            return httpx.Response(
                201,
                json={
                    "id": backup_id,
                    "checksum": checksum,
                    "size_bytes": len(contents),
                    "created_at": "2026-07-24T00:00:00Z",
                },
            )
        return httpx.Response(
            200,
            content=contents,
            headers={"X-Checksum-SHA256": checksum},
        )

    destination = download_installed_backup(
        install_root,
        admin_username="runtime-admin",
        admin_password="strong-password",  # noqa: S106
        output_directory=tmp_path / "backups",
        transport=httpx.MockTransport(handler),
    )

    assert destination.read_bytes() == contents
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_backup_deletes_download_when_integrity_check_fails(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    install_root = tmp_path / "installed"
    _write_installed_release(install_root, package, services=["runtime"])
    backup_id = str(uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/login":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "token_type": "bearer",
                },
            )
        if request.url.path == "/api/v1/admin/backups":
            return httpx.Response(
                201,
                json={
                    "id": backup_id,
                    "checksum": hashlib.sha256(b"expected").hexdigest(),
                    "size_bytes": len(b"tampered"),
                    "created_at": "2026-07-24T00:00:00Z",
                },
            )
        return httpx.Response(200, content=b"tampered")

    output = tmp_path / "backups"
    with pytest.raises(PackageValidationError, match="integrity verification"):
        download_installed_backup(
            install_root,
            admin_username="runtime-admin",
            admin_password="strong-password",  # noqa: S106
            output_directory=output,
            transport=httpx.MockTransport(handler),
        )

    assert list(output.iterdir()) == []


def test_acceptance_runs_signed_required_tests_and_sends_api_key(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        assert request.headers["x-api-key"] == "runtime-key"
        return httpx.Response(200, json={"answer": "hello", "metadata": {"version": "1.0.0"}})

    results = run_acceptance(
        package,
        base_url="https://runtime.internal",
        api_key="runtime-key",
        transport=httpx.MockTransport(handler),
    )

    assert [result["passed"] for result in results] == [True, True]


def test_acceptance_fails_when_required_response_does_not_match(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    transport = httpx.MockTransport(lambda _request: httpx.Response(503))

    with pytest.raises(PackageValidationError, match="Required acceptance test failed: health"):
        run_acceptance(
            package,
            base_url="https://runtime.internal",
            transport=transport,
        )


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


def test_restore_command_orchestrates_installed_compose_stack(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    install_root = tmp_path / "installed"
    release = _write_installed_release(
        install_root,
        package,
        services=[
            "runtime",
            "postgresql",
            "redis",
            "sandbox-controller",
            "sandbox-executor",
            "sandbox-gateway",
            "sandbox-egress-proxy",
        ],
    )
    backup = tmp_path / "runtime.unnest-backup"
    backup.write_bytes(b"encrypted backup")
    identity = tmp_path / "recovery.txt"
    identity.write_text("AGE-SECRET-KEY-test", encoding="utf-8")
    identity.chmod(0o600)
    commands: list[tuple[list[str], Path]] = []
    monkeypatch.setattr("langflow.unnestctl.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "langflow.unnestctl._run",
        lambda command, cwd: commands.append((command, cwd)),
    )

    result = CliRunner().invoke(
        app,
        [
            "restore",
            str(backup),
            "--identity",
            str(identity),
            "--install-root",
            str(install_root),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert all(cwd == release for _command, cwd in commands)
    assert commands[0][0][-7:] == [
        "stop",
        "runtime",
        "redis",
        "sandbox-controller",
        "sandbox-executor",
        "sandbox-gateway",
        "sandbox-egress-proxy",
    ]
    restore_command = commands[2][0]
    assert restore_command[6:11] == ["--profile", "maintenance", "run", "--rm", "--no-deps"]
    assert "--expected-release" in restore_command
    assert restore_command[restore_command.index("--expected-release") + 1] == "1.0.0"
    assert "--allow-group-readable-identity" in restore_command
    assert str(backup) not in restore_command
    assert str(identity) not in restore_command
    assert commands[-2][0][-4:] == ["-T", "redis", "redis-cli", "FLUSHALL"]
    assert commands[-1][0][-4:] == ["up", "-d", "--pull", "never"]
    assert list(release.glob(".unnest-restore-*")) == []


def test_restore_command_restarts_stack_after_maintenance_failure(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    install_root = tmp_path / "installed"
    _write_installed_release(install_root, package, services=["runtime", "postgresql", "redis"])
    backup = tmp_path / "runtime.unnest-backup"
    backup.write_bytes(b"encrypted backup")
    identity = tmp_path / "recovery.txt"
    identity.write_text("AGE-SECRET-KEY-test", encoding="utf-8")
    identity.chmod(0o600)
    commands: list[list[str]] = []

    def fail_restore(command: list[str], cwd: Path) -> None:
        del cwd
        commands.append(command)
        if "maintenance" in command:
            message = "maintenance failed"
            raise PackageValidationError(message)

    monkeypatch.setattr("langflow.unnestctl.platform.system", lambda: "Linux")
    monkeypatch.setattr("langflow.unnestctl._run", fail_restore)

    result = CliRunner().invoke(
        app,
        [
            "restore",
            str(backup),
            "--identity",
            str(identity),
            "--install-root",
            str(install_root),
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert commands[-1][-4:] == ["up", "-d", "--pull", "never"]


def test_restore_command_keeps_runtime_stopped_when_cache_reset_fails(tmp_path, monkeypatch):
    package = _write_signed_package(tmp_path / "package", monkeypatch)
    install_root = tmp_path / "installed"
    _write_installed_release(install_root, package, services=["runtime", "postgresql", "redis"])
    backup = tmp_path / "runtime.unnest-backup"
    backup.write_bytes(b"encrypted backup")
    identity = tmp_path / "recovery.txt"
    identity.write_text("AGE-SECRET-KEY-test", encoding="utf-8")
    identity.chmod(0o600)
    commands: list[list[str]] = []

    def fail_cache_reset(command: list[str], cwd: Path) -> None:
        del cwd
        commands.append(command)
        if "FLUSHALL" in command:
            message = "cache reset failed"
            raise PackageValidationError(message)

    monkeypatch.setattr("langflow.unnestctl.platform.system", lambda: "Linux")
    monkeypatch.setattr("langflow.unnestctl._run", fail_cache_reset)

    result = CliRunner().invoke(
        app,
        [
            "restore",
            str(backup),
            "--identity",
            str(identity),
            "--install-root",
            str(install_root),
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert commands[-1][-4:] == ["-T", "redis", "redis-cli", "FLUSHALL"]
