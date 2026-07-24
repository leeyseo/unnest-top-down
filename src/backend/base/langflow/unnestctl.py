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
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import typer
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa

from langflow.services.deployment.offline_package import (
    CHECKSUM_FILE,
    CHECKSUM_SIGNATURE_FILE,
    sha256_file,
)
from langflow.services.runtime_backup import RuntimeBackupError, restore_runtime_backup

if TYPE_CHECKING:
    from collections.abc import Iterator

app = typer.Typer(no_args_is_help=True, add_completion=False)
trust_app = typer.Typer(no_args_is_help=True)
app.add_typer(trust_app, name="trust")
SUPPORTED_ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
SHA256_HEX_LENGTH = 64
MAX_ACCEPTANCE_TESTS = 100
MIN_HTTP_STATUS = 100
MAX_HTTP_STATUS = 599
MAX_TCP_PORT = 65535
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_FILE_SIZE = 100 * 1024**3
MAX_ARCHIVE_TOTAL_SIZE = 500 * 1024**3
MAX_SIGNED_METADATA_SIZE = 16 * 1024**2
PACKAGE_LAYOUT_VERSION = 2
ASCII_CONTROL_END = 32
ASCII_DELETE = 127
_UNSIGNED_PACKAGE_FILES = frozenset({CHECKSUM_FILE, CHECKSUM_SIGNATURE_FILE})


class PackageValidationError(ValueError):
    pass


def _has_unsafe_path_characters(value: str) -> bool:
    return "\\" in value or any(
        ord(character) < ASCII_CONTROL_END or ord(character) == ASCII_DELETE for character in value
    )


def _package_file(root: Path, relative: str) -> Path:
    if (
        not relative
        or _has_unsafe_path_characters(relative)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise PackageValidationError(f"Unsafe package path: {relative}")
    path = root / relative
    if path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise PackageValidationError(f"Package path escapes its root: {relative}")
    return path


def _extract_package_archive(archive_path: Path, destination: Path) -> None:
    """Extract a release archive without allowing links, traversal, or special files."""
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            seen: set[Path] = set()
            total_size = 0
            for member_number, member in enumerate(archive, start=1):
                if member_number > MAX_ARCHIVE_MEMBERS:
                    raise PackageValidationError("Release archive contains too many entries")
                if member.size > MAX_ARCHIVE_FILE_SIZE:
                    raise PackageValidationError(f"Release archive entry is too large: {member.name}")
                total_size += member.size
                if total_size > MAX_ARCHIVE_TOTAL_SIZE:
                    raise PackageValidationError("Release archive is too large")
                if getattr(member, "sparse", None) or any(key.startswith("GNU.sparse") for key in member.pax_headers):
                    raise PackageValidationError(f"Sparse release archive entry is unsupported: {member.name}")
                relative = Path(member.name)
                if (
                    relative.is_absolute()
                    or not member.name
                    or member.name in {".", "./"}
                    or _has_unsafe_path_characters(member.name)
                    or ".." in relative.parts
                ):
                    raise PackageValidationError(f"Unsafe release archive path: {member.name}")
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise PackageValidationError(f"Unsupported release archive entry: {member.name}")
                target = destination / relative
                if relative in seen or destination.resolve() not in target.resolve().parents:
                    raise PackageValidationError(f"Duplicate or escaping release archive path: {member.name}")
                seen.add(relative)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=False)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise PackageValidationError(f"Release archive entry cannot be read: {member.name}")
                with source, target.open("xb") as destination_file:
                    shutil.copyfileobj(source, destination_file)
                if target.stat().st_size != member.size:
                    raise PackageValidationError(f"Release archive entry is truncated: {member.name}")
    except (OSError, tarfile.TarError) as exc:
        raise PackageValidationError("Release archive is missing or invalid") from exc


@contextmanager
def _materialize_package(package: Path) -> Iterator[Path]:
    """Yield a validated package directory, extracting tar input into a temporary root."""
    package = package.resolve()
    if package.is_dir() and not package.is_symlink():
        yield package
        return
    if not package.is_file() or package.is_symlink():
        raise PackageValidationError("Release package must be a directory or tar archive")
    with TemporaryDirectory(prefix="unnest-release-") as temporary:
        root = Path(temporary)
        _extract_package_archive(package, root)
        yield root


def load_manifest(package: Path) -> dict[str, Any]:
    manifest_path = package / "manifest" / "release.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("Release manifest is missing or invalid") from exc
    if not isinstance(value, dict) or value.get("provider") != "unnest-on-prem":
        raise PackageValidationError("Unsupported release manifest")
    return value


def _package_regular_files(package: Path) -> set[str]:
    files: set[str] = set()
    for path in package.rglob("*"):
        relative = path.relative_to(package).as_posix()
        mode = path.lstat().st_mode
        if path.is_symlink() or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise PackageValidationError(f"Unsupported package entry: {relative}")
        if stat.S_ISREG(mode):
            files.add(relative)
    return files


def verify_checksums(package: Path) -> set[str]:
    checksum_path = package / CHECKSUM_FILE
    try:
        if checksum_path.stat().st_size > MAX_SIGNED_METADATA_SIZE:
            raise PackageValidationError("checksums.sha256 is too large")
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
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
        if relative in _UNSIGNED_PACKAGE_FILES:
            raise PackageValidationError(f"Checksum entry is not allowed: {relative}")
        if relative in checked:
            raise PackageValidationError(f"Duplicate checksum entry: {relative}")
        if len(digest) != SHA256_HEX_LENGTH or any(character not in "0123456789abcdef" for character in digest):
            raise PackageValidationError(f"Invalid SHA-256 digest for {relative}")
        path = _package_file(package, relative)
        if not path.is_file():
            raise PackageValidationError(f"Checksummed file is missing: {relative}")
        if sha256_file(path) != digest:
            raise PackageValidationError(f"Checksum mismatch: {relative}")
        checked.add(relative)
    expected = _package_regular_files(package).difference(_UNSIGNED_PACKAGE_FILES)
    if checked != expected:
        missing = sorted(expected.difference(checked))
        unexpected = sorted(checked.difference(expected))
        detail = f"missing={missing}, unexpected={unexpected}"
        raise PackageValidationError(f"Checksum file set does not match package files: {detail}")
    return checked


def _public_key_fingerprint(public_key_blob: bytes) -> str:
    try:
        public_key = serialization.load_pem_public_key(public_key_blob)
        if not isinstance(
            public_key,
            (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey, ec.EllipticCurvePublicKey, rsa.RSAPublicKey),
        ):
            raise TypeError
        der = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (ValueError, TypeError) as exc:
        raise PackageValidationError("Trusted public key is invalid") from exc
    return f"sha256:{hashlib.sha256(der).hexdigest()}"


def enroll_release_key(public_key_path: Path, trust_directory: Path) -> str:
    """Enroll an SI release public key under its SPKI SHA-256 fingerprint."""
    if public_key_path.is_symlink() or not public_key_path.is_file():
        raise PackageValidationError("SI release public key is missing or invalid")
    key_blob = _read_limited(public_key_path, label="SI release public key")
    fingerprint = _public_key_fingerprint(key_blob)
    trust_directory.mkdir(parents=True, exist_ok=True)
    destination = trust_directory / f"{fingerprint.removeprefix('sha256:')}.pem"
    if destination.exists():
        if destination.is_symlink() or _public_key_fingerprint(destination.read_bytes()) != fingerprint:
            raise PackageValidationError("Trusted SI release key destination is invalid")
        return fingerprint
    try:
        with destination.open("xb") as enrolled:
            enrolled.write(key_blob)
        destination.chmod(0o644)
    except OSError as exc:
        raise PackageValidationError("SI release public key could not be enrolled") from exc
    return fingerprint


def _verify_blob_signature(public_key_blob: bytes, signature_blob: bytes, blob: bytes) -> None:
    try:
        public_key = serialization.load_pem_public_key(public_key_blob)
        signature = base64.b64decode(signature_blob.strip(), validate=True)
        if isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(signature, blob)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, blob, ec.ECDSA(hashes.SHA256()))
        elif isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, blob, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise TypeError
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise PackageValidationError("Signature verification failed") from exc


def _read_limited(path: Path, *, label: str) -> bytes:
    try:
        if path.stat().st_size > MAX_SIGNED_METADATA_SIZE:
            raise PackageValidationError(f"{label} is too large")
        return path.read_bytes()
    except OSError as exc:
        raise PackageValidationError(f"{label} is missing") from exc


def _verify_release_signature(package: Path, trust_directory: Path) -> str:
    checksums = _read_limited(package / CHECKSUM_FILE, label=CHECKSUM_FILE)
    signature = _read_limited(package / CHECKSUM_SIGNATURE_FILE, label=CHECKSUM_SIGNATURE_FILE)
    try:
        candidates = sorted(trust_directory.glob("*.pem"))
    except OSError as exc:
        raise PackageValidationError("Release trust directory is unavailable") from exc
    if not candidates:
        raise PackageValidationError("No trusted SI release key is enrolled")
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            key_blob = candidate.read_bytes()
            fingerprint = _public_key_fingerprint(key_blob)
            if candidate.name != f"{fingerprint.removeprefix('sha256:')}.pem":
                continue
            _verify_blob_signature(key_blob, signature, checksums)
        except PackageValidationError:
            continue
        return fingerprint
    raise PackageValidationError("Package is not signed by an enrolled SI release key")


def verify_license(package: Path, manifest: dict[str, Any], public_key_path: Path) -> None:
    license_path = package / "license" / "license.json"
    license_blob = _read_limited(license_path, label="Offline license")
    public_key_blob = _read_limited(public_key_path, label="Trusted vendor license public key")
    signature_blob = _read_limited(package / "license" / "license.sig", label="Offline license signature")
    _verify_blob_signature(public_key_blob, signature_blob, license_blob)
    try:
        license_data = json.loads(license_blob)
        expires_at = datetime.fromisoformat(str(license_data["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PackageValidationError("Offline license is invalid") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise PackageValidationError("Offline license has expired")
    if license_data.get("release_digest") != manifest.get("release_digest"):
        raise PackageValidationError("Offline license does not permit this exact release")


def _verify_package_directory(
    package: Path,
    *,
    release_trust_directory: Path | None = None,
    license_public_key: Path | None = None,
) -> dict[str, Any]:
    package = package.resolve()
    trust_directory = release_trust_directory or Path(
        os.getenv("UNNEST_RELEASE_TRUST_DIR", "/etc/unnest/trust/releases")
    )
    vendor_key = license_public_key or Path(
        os.getenv("UNNEST_LICENSE_PUBLIC_KEY", "/etc/unnest/trust/vendor-license.pem")
    )
    signer_fingerprint = _verify_release_signature(package, trust_directory)
    checked = verify_checksums(package)
    manifest = load_manifest(package)
    if manifest.get("package", {}).get("layout_version") != PACKAGE_LAYOUT_VERSION:
        raise PackageValidationError("Unsupported release package layout")
    if manifest.get("build", {}).get("signing_enabled") is not True:
        raise PackageValidationError("Release signing is mandatory")
    if manifest.get("build", {}).get("signer_fingerprint") != signer_fingerprint:
        raise PackageValidationError("Release signer fingerprint does not match the trusted signature")
    if package.joinpath("keys/cosign.pub").exists() or package.joinpath("keys/license.pub").exists():
        raise PackageValidationError("Package must not supply its own trust keys")
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
    if missing_checksums := sorted(required_paths.difference(checked)):
        raise PackageValidationError(f"Required files are not checksummed: {', '.join(missing_checksums)}")
    acceptance_path = package / "tests" / "acceptance.json"
    try:
        acceptance_tests = json.loads(acceptance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("Acceptance tests are missing or invalid") from exc
    if not isinstance(acceptance_tests, list) or not acceptance_tests or len(acceptance_tests) > MAX_ACCEPTANCE_TESTS:
        raise PackageValidationError("Acceptance tests must be a non-empty list of at most 100 tests")
    if acceptance_tests != manifest.get("acceptance_tests"):
        raise PackageValidationError("Acceptance tests do not match the signed release manifest")
    verify_license(package, manifest, vendor_key)
    return manifest


def verify_package(
    package: Path,
    *,
    release_trust_directory: Path | None = None,
    license_public_key: Path | None = None,
) -> dict[str, Any]:
    """Verify an unpacked release directory or a tar release archive."""
    with _materialize_package(package) as root:
        return _verify_package_directory(
            root,
            release_trust_directory=release_trust_directory,
            license_public_key=license_public_key,
        )


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


def _tcp_port_available(port: int, host: str = "") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
    except PermissionError:
        # Docker/Podman may be allowed to publish a privileged port even when
        # the invoking unprivileged user cannot bind it directly.
        return True
    except OSError:
        return False
    return True


def _preflight_directory(package: Path) -> list[str]:
    manifest = _verify_package_directory(package)
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
    host_ports: list[int] = []
    for binding in manifest.get("ports", []):
        if not isinstance(binding, dict):
            raise PackageValidationError("Release manifest contains an invalid port binding")
        port = binding.get("port")
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= MAX_TCP_PORT
            or binding.get("protocol") != "tcp"
            or binding.get("scope") not in {"host", "internal"}
        ):
            raise PackageValidationError("Release manifest contains an invalid port binding")
        if binding["scope"] == "host":
            host_ports.append(port)
    occupied = sorted({port for port in host_ports if not _tcp_port_available(port)})
    if occupied:
        raise PackageValidationError(f"Required host ports are unavailable: {', '.join(map(str, occupied))}")
    return [
        f"release={manifest.get('release_version')}",
        f"architecture={architecture}",
        f"orchestrator={orchestrator}",
    ]


def preflight(package: Path) -> list[str]:
    """Run host and package checks for an unpacked directory or tar archive."""
    with _materialize_package(package) as root:
        return _preflight_directory(root)


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


def _run_acceptance_directory(
    package: Path,
    *,
    base_url: str,
    api_key: str | None = None,
    ca: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    manifest = _verify_package_directory(package)
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


def run_acceptance(
    package: Path,
    *,
    base_url: str,
    api_key: str | None = None,
    ca: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Run release acceptance tests against an unpacked directory or tar archive."""
    with _materialize_package(package) as root:
        return _run_acceptance_directory(
            root,
            base_url=base_url,
            api_key=api_key,
            ca=ca,
            transport=transport,
        )


@trust_app.command("import")
def import_release_key(
    public_key: Path = typer.Argument(..., exists=True, dir_okay=False),
    trust_directory: Path = typer.Option(
        Path("/etc/unnest/trust/releases"),
        "--trust-dir",
        envvar="UNNEST_RELEASE_TRUST_DIR",
    ),
) -> None:
    fingerprint = enroll_release_key(public_key, trust_directory)
    typer.echo(f"trusted SI release key {fingerprint}")


@app.command()
def verify(package: Path = typer.Argument(..., exists=True)) -> None:
    manifest = verify_package(package)
    typer.echo(f"verified release {manifest.get('release_version')}")


@app.command("preflight")
def check(package: Path = typer.Argument(..., exists=True)) -> None:
    for item in preflight(package):
        typer.echo(item)


@app.command()
def acceptance(
    package: Path = typer.Argument(..., exists=True),
    url: str = typer.Option(..., "--url"),
    api_key: str | None = typer.Option(None, envvar="UNNEST_API_KEY", hidden=True),
    ca: Path | None = typer.Option(None, "--ca", exists=True, dir_okay=False),
) -> None:
    for result in run_acceptance(package, base_url=url, api_key=api_key, ca=ca):
        state = "PASS" if result["passed"] else "WARN"
        typer.echo(f"{state} {result['name']}: {result['detail']}")


@app.command()
def install(package: Path = typer.Argument(..., exists=True)) -> None:
    with _materialize_package(package) as root:
        manifest = _verify_package_directory(root)
        _preflight_directory(root)
        deployment = manifest.get("deployment", {})
        if deployment.get("orchestrator") == "helm":
            _run(
                [
                    "helm",
                    "upgrade",
                    "--install",
                    "unnest",
                    "helm/unnest",
                    "--namespace",
                    "unnest",
                    "--create-namespace",
                ],
                root,
            )
        else:
            runtime = "docker" if shutil.which("docker") else "podman"
            for image in sorted((root / "images").glob("*.tar")):
                _run([runtime, "load", "--input", str(image)], root)
            _run([runtime, "compose", "-f", "compose/compose.yml", "up", "-d"], root)
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
