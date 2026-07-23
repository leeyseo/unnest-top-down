# ruff: noqa: EM101, EM102, TRY003
"""Offline verification and preflight commands for an Unnest runtime package."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import platform
import shutil
import socket
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import typer
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa

from langflow.services.runtime_backup import RuntimeBackupError, restore_runtime_backup

app = typer.Typer(no_args_is_help=True, add_completion=False)
SUPPORTED_ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
SHA256_HEX_LENGTH = 64
MAX_ACCEPTANCE_TESTS = 100
MIN_HTTP_STATUS = 100
MAX_HTTP_STATUS = 599


class PackageValidationError(ValueError):
    pass


def _package_file(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise PackageValidationError(f"Unsafe package path: {relative}")
    path = root / relative
    if path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise PackageValidationError(f"Package path escapes its root: {relative}")
    return path


def load_manifest(package: Path) -> dict[str, Any]:
    manifest_path = package / "manifest" / "release.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("Release manifest is missing or invalid") from exc
    if not isinstance(value, dict) or value.get("provider") != "unnest-on-prem":
        raise PackageValidationError("Unsupported release manifest")
    return value


def verify_checksums(package: Path) -> set[str]:
    checksum_path = package / "checksums.sha256"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PackageValidationError("checksums.sha256 is missing") from exc
    if not lines:
        raise PackageValidationError("checksums.sha256 is empty")
    checked: set[str] = set()
    for line in lines:
        try:
            digest, relative = line.split(maxsplit=1)
        except ValueError as exc:
            raise PackageValidationError("Invalid checksum entry") from exc
        relative = relative.removeprefix("*")
        if len(digest) != SHA256_HEX_LENGTH or any(character not in "0123456789abcdef" for character in digest):
            raise PackageValidationError(f"Invalid SHA-256 digest for {relative}")
        path = _package_file(package, relative)
        if not path.is_file():
            raise PackageValidationError(f"Checksummed file is missing: {relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise PackageValidationError(f"Checksum mismatch: {relative}")
        checked.add(relative)
    return checked


def _verify_blob_signature(public_key_path: Path, signature_path: Path, blob: bytes) -> None:
    try:
        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
        signature = base64.b64decode(signature_path.read_text(encoding="utf-8").strip(), validate=True)
        if isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(signature, blob)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, blob, ec.ECDSA(hashes.SHA256()))
        elif isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, blob, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise TypeError
    except (OSError, InvalidSignature, ValueError, TypeError) as exc:
        raise PackageValidationError(f"Signature verification failed: {signature_path.name}") from exc


def verify_license(package: Path, manifest: dict[str, Any]) -> None:
    license_path = package / "license" / "license.json"
    try:
        license_blob = license_path.read_bytes()
    except OSError as exc:
        raise PackageValidationError("Offline license is missing") from exc
    _verify_blob_signature(
        package / "keys" / "license.pub",
        package / "license" / "license.sig",
        license_blob,
    )
    try:
        license_data = json.loads(license_path.read_text(encoding="utf-8"))
        expires_at = datetime.fromisoformat(str(license_data["expires_at"]).replace("Z", "+00:00"))
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PackageValidationError("Offline license is invalid") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise PackageValidationError("Offline license has expired")
    release_versions = license_data.get("release_versions", [])
    if release_versions and manifest.get("release_version") not in release_versions:
        raise PackageValidationError("Offline license does not permit this release")


def verify_package(package: Path) -> dict[str, Any]:
    package = package.resolve()
    manifest = load_manifest(package)
    contract = manifest.get("package", {})
    required_paths: set[str] = set()
    for relative in contract.get("required_files", []):
        if not isinstance(relative, str) or not _package_file(package, relative).is_file():
            raise PackageValidationError(f"Required package file is missing: {relative}")
        required_paths.add(relative)
    for pattern in contract.get("required_globs", []):
        matches = list(package.glob(pattern)) if isinstance(pattern, str) and ".." not in Path(pattern).parts else []
        validated_matches = [
            path for path in matches if _package_file(package, str(path.relative_to(package))).is_file()
        ]
        if not validated_matches:
            raise PackageValidationError(f"Required package content is missing: {pattern}")
        required_paths.update(str(path.relative_to(package)) for path in validated_matches)
    checked = verify_checksums(package)
    if missing_checksums := sorted(required_paths.difference(checked)):
        raise PackageValidationError(f"Required files are not checksummed: {', '.join(missing_checksums)}")
    manifest_path = package / "manifest" / "release.json"
    if manifest.get("build", {}).get("signing_enabled") is True:
        _verify_blob_signature(
            package / "keys" / "cosign.pub",
            package / "signatures" / "release-manifest.sig",
            manifest_path.read_bytes(),
        )
        _verify_blob_signature(
            package / "keys" / "cosign.pub",
            package / "signatures" / "checksums.sig",
            (package / "checksums.sha256").read_bytes(),
        )
    acceptance_path = package / "tests" / "acceptance.json"
    try:
        acceptance_tests = json.loads(acceptance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("Acceptance tests are missing or invalid") from exc
    if not isinstance(acceptance_tests, list) or not acceptance_tests or len(acceptance_tests) > MAX_ACCEPTANCE_TESTS:
        raise PackageValidationError("Acceptance tests must be a non-empty list of at most 100 tests")
    if acceptance_tests != manifest.get("acceptance_tests"):
        raise PackageValidationError("Acceptance tests do not match the signed release manifest")
    verify_license(package, manifest)
    return manifest


def _available_memory() -> int:
    return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))


def _endpoint_ready(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname or "", port), timeout=3):
            return True
    except OSError:
        return False


def preflight(package: Path) -> list[str]:
    manifest = verify_package(package)
    if platform.system() != "Linux":
        raise PackageValidationError("Only Linux is supported")
    architecture = SUPPORTED_ARCHITECTURES.get(platform.machine().lower())
    expected_architecture = manifest.get("deployment", {}).get("architecture")
    if architecture != expected_architecture:
        raise PackageValidationError(
            f"Package architecture is {expected_architecture}, host architecture is {architecture or 'unsupported'}"
        )

    resources = manifest.get("deployment", {}).get("resources", {})
    required_cpu = math.ceil(float(resources.get("cpu", 0)))
    required_memory = int(resources.get("memory_bytes", 0))
    required_disk = int(resources.get("disk_bytes", 0))
    if (os.cpu_count() or 0) < required_cpu:
        raise PackageValidationError(f"At least {required_cpu} CPU cores are required")
    if _available_memory() < required_memory:
        raise PackageValidationError(f"At least {required_memory} bytes of RAM are required")
    if shutil.disk_usage(package).free < required_disk:
        raise PackageValidationError(f"At least {required_disk} bytes of free disk are required")

    orchestrator = manifest.get("deployment", {}).get("orchestrator")
    required_command = "helm" if orchestrator == "helm" else "docker"
    if shutil.which(required_command) is None and not (orchestrator == "compose" and shutil.which("podman")):
        raise PackageValidationError(f"{required_command} or a supported alternative is required")
    if manifest.get("deployment", {}).get("accelerator") == "nvidia" and shutil.which("nvidia-smi") is None:
        raise PackageValidationError("NVIDIA driver tooling is unavailable")
    if manifest.get("deployment", {}).get("accelerator") == "amd" and shutil.which("rocminfo") is None:
        raise PackageValidationError("AMD ROCm tooling is unavailable")

    unreachable = [endpoint for endpoint in manifest.get("external_endpoints", []) if not _endpoint_ready(endpoint)]
    if unreachable:
        raise PackageValidationError(f"Required endpoints are unreachable: {', '.join(unreachable)}")
    return [
        f"release={manifest.get('release_version')}",
        f"architecture={architecture}",
        f"orchestrator={orchestrator}",
    ]


def _run(command: list[str], cwd: Path) -> None:
    import subprocess

    completed = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603
    if completed.returncode:
        raise PackageValidationError(f"Command failed with exit code {completed.returncode}: {command[0]}")


def _contains_expected(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains_expected(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _contains_expected(actual_value, expected_value)
                for actual_value, expected_value in zip(actual, expected, strict=True)
            )
        )
    return actual == expected


def run_acceptance(
    package: Path,
    *,
    base_url: str,
    api_key: str | None = None,
    ca: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    manifest = verify_package(package)
    parsed_base = urlparse(base_url)
    if (
        parsed_base.scheme not in {"http", "https"}
        or not parsed_base.hostname
        or parsed_base.username
        or parsed_base.password
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise PackageValidationError("Runtime URL must be an HTTP(S) URL without credentials, query, or fragment")
    verify: bool | str = str(ca) if ca else True
    headers = {"x-api-key": api_key} if api_key else {}
    results: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        headers=headers,
        verify=verify,
        transport=transport,
        timeout=30,
    ) as client:
        for test in manifest["acceptance_tests"]:
            name = test.get("name")
            request = test.get("request")
            expected = test.get("expected")
            required = test.get("required", True)
            if not isinstance(name, str) or not isinstance(request, dict) or not isinstance(expected, dict):
                raise PackageValidationError("Acceptance test contract is invalid")
            path = request.get("path")
            parsed_path = urlparse(path) if isinstance(path, str) else None
            if (
                parsed_path is None
                or not path.startswith("/")
                or path.startswith("//")
                or ".." in Path(parsed_path.path).parts
                or parsed_path.scheme
                or parsed_path.netloc
                or parsed_path.query
                or parsed_path.fragment
            ):
                raise PackageValidationError(f"Acceptance test has an unsafe path: {name}")
            expected_status = expected.get("status")
            if not isinstance(expected_status, int) or not MIN_HTTP_STATUS <= expected_status <= MAX_HTTP_STATUS:
                raise PackageValidationError(f"Acceptance test has an invalid expected status: {name}")
            method = str(request.get("method") or ("POST" if "body" in request else "GET")).upper()
            if method not in {"GET", "POST"}:
                raise PackageValidationError(f"Acceptance test has an unsupported method: {name}")
            try:
                response = client.request(method, path, json=request.get("body") if "body" in request else None)
            except httpx.HTTPError as exc:
                if required:
                    raise PackageValidationError(f"Required acceptance test could not connect: {name}") from exc
                results.append({"name": name, "required": False, "passed": False, "detail": type(exc).__name__})
                continue
            passed = response.status_code == expected_status
            detail = f"status={response.status_code}"
            if passed and "body" in expected:
                try:
                    actual_body = response.json()
                except ValueError:
                    passed = False
                    detail = "response body is not JSON"
                else:
                    passed = _contains_expected(actual_body, expected["body"])
                    if not passed:
                        detail = "response body did not match"
            result = {"name": name, "required": bool(required), "passed": passed, "detail": detail}
            results.append(result)
            if required and not passed:
                raise PackageValidationError(f"Required acceptance test failed: {name} ({detail})")
    return results


@app.command()
def verify(package: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    manifest = verify_package(package)
    typer.echo(f"verified release {manifest.get('release_version')}")


@app.command("preflight")
def check(package: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    for item in preflight(package):
        typer.echo(item)


@app.command()
def acceptance(
    package: Path = typer.Argument(..., exists=True, file_okay=False),
    url: str = typer.Option(..., "--url"),
    api_key: str | None = typer.Option(None, envvar="UNNEST_API_KEY", hidden=True),
    ca: Path | None = typer.Option(None, "--ca", exists=True, dir_okay=False),
) -> None:
    for result in run_acceptance(package, base_url=url, api_key=api_key, ca=ca):
        state = "PASS" if result["passed"] else "WARN"
        typer.echo(f"{state} {result['name']}: {result['detail']}")


@app.command()
def install(package: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    manifest = verify_package(package)
    preflight(package)
    deployment = manifest.get("deployment", {})
    if deployment.get("orchestrator") == "helm":
        _run(
            ["helm", "upgrade", "--install", "unnest", "helm/unnest", "--namespace", "unnest", "--create-namespace"],
            package,
        )
    else:
        runtime = "docker" if shutil.which("docker") else "podman"
        for image in sorted((package / "images").glob("*.tar")):
            _run([runtime, "load", "--input", str(image)], package)
        _run([runtime, "compose", "-f", "compose/compose.yml", "up", "-d"], package)
    typer.echo("installed; open the runtime URL shown by your deployment profile to complete initial setup")


@app.command()
def restore(
    backup: Path = typer.Argument(..., exists=True, dir_okay=False),
    identity_file: Path = typer.Option(..., "--identity", exists=True, dir_okay=False),
    database_url: str = typer.Option(..., envvar="LANGFLOW_DATABASE_URL"),
    storage_directory: Path = typer.Option(Path("/opt/unnest/data"), "--storage-dir"),
    master_key: Path = typer.Option(Path("/opt/unnest/secrets/master.key"), "--master-key"),
    license_directory: Path = typer.Option(Path("/opt/unnest/license"), "--license-dir"),
    key_directory: Path = typer.Option(Path("/opt/unnest/keys"), "--key-dir"),
    runtime_stopped: bool = typer.Option(False, "--runtime-stopped"),  # noqa: FBT001, FBT003
    yes: bool = typer.Option(False, "--yes"),  # noqa: FBT001, FBT003
) -> None:
    if platform.system() != "Linux":
        raise PackageValidationError("Only Linux is supported")
    if not runtime_stopped:
        raise PackageValidationError(
            "Stop every Runtime, scheduler, and worker instance before restore, then pass --runtime-stopped"
        )
    if stat.S_IMODE(identity_file.stat().st_mode) & 0o077:
        raise PackageValidationError("Recovery identity file must have mode 0600")
    if not yes and not typer.confirm("Restore will replace runtime database, files, and keys. Continue?"):
        raise typer.Abort
    try:
        result = restore_runtime_backup(
            path=backup,
            identity=identity_file.read_text(encoding="utf-8").strip(),
            database_url=database_url,
            storage_directory=storage_directory,
            master_key_destination=master_key,
            license_directory=license_directory,
            key_directory=key_directory,
        )
    except (OSError, RuntimeBackupError, ValueError) as exc:
        raise PackageValidationError("Runtime restore failed verification or was rolled back") from exc
    typer.echo(
        f"restored backup {result.backup_id} for release {result.release_version}; "
        "run database migrations and acceptance tests before restarting traffic"
    )


def main() -> None:
    try:
        app()
    except PackageValidationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    main()
