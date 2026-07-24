# ruff: noqa: EM101, EM102, TRY003
"""Offline verification and preflight commands for an Unnest runtime package."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import tarfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import typer
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from langflow.services.deployment.offline_dependencies import DependencyLockError, verify_locked_wheels
from langflow.services.deployment.offline_package import (
    CHECKSUM_FILE,
    CHECKSUM_SIGNATURE_FILE,
    sha256_file,
)
from langflow.services.deployment.source_documents import (
    SourceDocumentError,
    verify_bundled_source_documents,
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
MIN_ROOT_FLOWS = 2
MIN_HTTP_STATUS = 100
MAX_HTTP_STATUS = 599
MAX_TCP_PORT = 65535
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_FILE_SIZE = 30 * 1024**3
MAX_ARCHIVE_TOTAL_SIZE = 50 * 1024**3
MAX_SIGNED_METADATA_SIZE = 16 * 1024**2
PACKAGE_LAYOUT_VERSION = 2
ASCII_CONTROL_END = 32
ASCII_DELETE = 127
IDENTITY_PRIVATE_MODE = 0o600
IDENTITY_GROUP_READABLE_MODE = 0o640
MAX_DNS_NAME_LENGTH = 253
_UNSIGNED_PACKAGE_FILES = frozenset({CHECKSUM_FILE, CHECKSUM_SIGNATURE_FILE})
_REQUIRED_LAYOUT_FILES = frozenset(
    {
        "manifest/release.json",
        "openapi/openapi.json",
        "compose/compose.yml",
        "images/unnest-runtime.tar",
        "images/postgresql.tar",
        "images/redis.tar",
        "reports/sbom.cdx.json",
        "reports/trivy.json",
        "tests/acceptance.json",
        "license/license.json",
        "license/license.sig",
        "wheels/requirements.lock",
        CHECKSUM_SIGNATURE_FILE,
    }
)
_MVP_PROFILE = {
    "orchestrator": "compose",
    "topology": "single",
    "architecture": "amd64",
    "accelerator": "cpu",
    "database": "embedded-postgresql",
    "storage": "local",
    "infrastructure": "bundled",
    "model_runtime": "external",
}
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


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
        archive_size = archive_path.stat().st_size
        if archive_size > MAX_ARCHIVE_TOTAL_SIZE or shutil.disk_usage(destination).free < archive_size:
            raise PackageValidationError("Release archive exceeds the safe extraction capacity")
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


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


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


def public_key_fingerprint(public_key_blob: bytes) -> str:
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
    fingerprint = public_key_fingerprint(key_blob)
    trust_directory.mkdir(parents=True, exist_ok=True)
    destination = trust_directory / f"{fingerprint.removeprefix('sha256:')}.pem"
    if destination.exists():
        if destination.is_symlink() or public_key_fingerprint(destination.read_bytes()) != fingerprint:
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
            fingerprint = public_key_fingerprint(key_blob)
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


def _validate_layout(package: Path, manifest: dict[str, Any], checked: set[str]) -> None:
    deployment = manifest.get("deployment")
    if not isinstance(deployment, dict) or any(deployment.get(name) != value for name, value in _MVP_PROFILE.items()):
        raise PackageValidationError("Release does not use the supported offline MVP profile")
    for relative in _REQUIRED_LAYOUT_FILES:
        if not _package_file(package, relative).is_file():
            raise PackageValidationError(f"Required package file is missing: {relative}")

    image_entries = manifest.get("images")
    expected_archives = {
        "images/unnest-runtime.tar",
        "images/postgresql.tar",
        "images/redis.tar",
    }
    if (
        not isinstance(image_entries, list)
        or len(image_entries) != len(expected_archives)
        or {entry.get("archive") for entry in image_entries if isinstance(entry, dict)} != expected_archives
        or {path.relative_to(package).as_posix() for path in (package / "images").glob("*.tar")} != expected_archives
    ):
        raise PackageValidationError("Release manifest must declare the three offline image archives")
    for entry in image_entries:
        if not isinstance(entry, dict):
            raise PackageValidationError("Release manifest contains an invalid image entry")
        archive = entry.get("archive")
        expected_digest = entry.get("archive_digest")
        image_digest = entry.get("image_digest")
        reference = entry.get("reference")
        if (
            not isinstance(archive, str)
            or not isinstance(expected_digest, str)
            or expected_digest != f"sha256:{sha256_file(_package_file(package, archive))}"
        ):
            raise PackageValidationError(f"Release image archive digest is invalid: {archive}")
        if (
            not isinstance(image_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)
            or not isinstance(reference, str)
            or not reference.endswith(f"@{image_digest}")
        ):
            raise PackageValidationError(f"Release image reference is not digest-pinned: {archive}")

    flow_entries = manifest.get("flows")
    if not isinstance(flow_entries, list) or len(flow_entries) < MIN_ROOT_FLOWS:
        raise PackageValidationError("Release manifest must declare Agent and Ingestion Flow Versions")
    roles = [entry.get("role") for entry in flow_entries if isinstance(entry, dict)]
    if roles.count("agent") != 1 or roles.count("ingestion") != 1:
        raise PackageValidationError("Release manifest requires exactly one Agent and Ingestion Flow Version")
    expected_flow_files: set[str] = set()
    for entry in flow_entries:
        version_id = entry.get("id") if isinstance(entry, dict) else None
        relative = f"flows/{version_id}.json"
        expected_flow_files.add(relative)
        flow_path = _package_file(package, relative)
        try:
            flow = json.loads(flow_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PackageValidationError(f"Bundled Flow Version is missing or invalid: {version_id}") from exc
        if _canonical_digest(flow) != entry.get("digest"):
            raise PackageValidationError(f"Bundled Flow Version digest mismatch: {version_id}")
        if relative not in checked:
            raise PackageValidationError(f"Bundled Flow Version is not checksummed: {version_id}")
    actual_flow_files = {path.relative_to(package).as_posix() for path in (package / "flows").glob("*.json")}
    if actual_flow_files != expected_flow_files:
        raise PackageValidationError("Bundled Flow Version files do not exactly match the release manifest")

    try:
        verify_locked_wheels(package, manifest)
    except DependencyLockError as exc:
        raise PackageValidationError(str(exc)) from exc
    try:
        verify_bundled_source_documents(package, manifest)
    except SourceDocumentError as exc:
        raise PackageValidationError(str(exc)) from exc


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
    _validate_layout(package, manifest, checked)
    contract = manifest.get("package", {})
    required_paths: set[str] = set()
    for relative in contract.get("required_files", []):
        if not isinstance(relative, str) or not _package_file(package, relative).is_file():
            raise PackageValidationError(f"Required package file is missing: {relative}")
        required_paths.add(relative)
    for pattern in contract.get("required_globs", []):
        matches = (
            list(package.glob(pattern))
            if isinstance(pattern, str)
            and not Path(pattern).is_absolute()
            and not _has_unsafe_path_characters(pattern)
            and ".." not in Path(pattern).parts
            else []
        )
        validated_matches = [
            path for path in matches if _package_file(package, str(path.relative_to(package))).is_file()
        ]
        if not validated_matches:
            raise PackageValidationError(f"Required package content is missing: {pattern}")
        required_paths.update(str(path.relative_to(package)) for path in validated_matches)
    if missing_checksums := sorted(required_paths.difference(checked).difference(_UNSIGNED_PACKAGE_FILES)):
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


def _rocky_linux_9() -> bool:
    try:
        values = {
            key: value.strip().strip('"')
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
    except OSError:
        return False
    return values.get("ID") == "rocky" and values.get("VERSION_ID", "").split(".", 1)[0] == "9"


def _docker_compose_available() -> bool:
    import subprocess

    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        completed = subprocess.run(  # noqa: S603
            [docker, "compose", "version"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _preflight_directory(package: Path) -> list[str]:
    manifest = _verify_package_directory(package)
    if platform.system() != "Linux":
        raise PackageValidationError("Only Linux is supported")
    if not _rocky_linux_9():
        raise PackageValidationError("Rocky Linux 9.x is required")
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
    if shutil.which("docker") is None or not _docker_compose_available():
        raise PackageValidationError("Docker Engine with the Docker Compose plugin is required")
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


def _default_server_name() -> str:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
            if item[4][0] not in {"127.0.0.1", "::1"}
        }
    except OSError:
        addresses = set()
    return sorted(addresses)[0] if addresses else "127.0.0.1"


def _certificate_name(server_name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(server_name))
    except ValueError:
        if len(server_name) > MAX_DNS_NAME_LENGTH or not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
            server_name,
        ):
            raise PackageValidationError("Server name must be an IP address or valid DNS name") from None
        return x509.DNSName(server_name)


def _write_tls_material(
    destination: Path,
    *,
    server_name: str,
    certificate: Path | None,
    private_key: Path | None,
) -> None:
    destination.mkdir(parents=True)
    certificate_destination = destination / "server.crt"
    key_destination = destination / "server.key"
    if (certificate is None) != (private_key is None):
        raise PackageValidationError("Institution TLS certificate and private key must be supplied together")
    if certificate is not None and private_key is not None:
        try:
            certificate_blob = certificate.read_bytes()
            key_blob = private_key.read_bytes()
            parsed_certificate = x509.load_pem_x509_certificate(certificate_blob)
            parsed_key = serialization.load_pem_private_key(key_blob, password=None)
            certificate_public_key = parsed_certificate.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            private_public_key = parsed_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise PackageValidationError("Institution TLS material is invalid") from exc
        if certificate_public_key != private_public_key:
            raise PackageValidationError("Institution TLS certificate and private key do not match")
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_name[:64])])
        now = datetime.now(timezone.utc)
        parsed_certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([_certificate_name(server_name)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        certificate_blob = parsed_certificate.public_bytes(serialization.Encoding.PEM)
        key_blob = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    certificate_destination.write_bytes(certificate_blob)
    key_destination.write_bytes(key_blob)
    certificate_destination.chmod(0o644)
    if os.geteuid() == 0:
        os.chown(destination, 0, 0)
        os.chown(key_destination, 0, 0)
    destination.chmod(0o750)
    key_destination.chmod(0o640)


def _write_sandbox_mtls_material(destination: Path) -> None:
    """Create a local CA with separate Runtime-client and Worker-server keys."""
    runtime_directory = destination / "runtime"
    worker_directory = destination / "worker"
    runtime_directory.mkdir(parents=True)
    worker_directory.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unnest sandbox local CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def issue(common_name: str, usage: ExtendedKeyUsageOID, *, server: bool) -> tuple[bytes, bytes]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca_subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=True)
        )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName("sandbox-gateway")]),
                critical=False,
            )
        certificate = builder.sign(ca_key, hashes.SHA256())
        return (
            certificate.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )

    server_certificate, server_key = issue(
        "sandbox-gateway",
        ExtendedKeyUsageOID.SERVER_AUTH,
        server=True,
    )
    client_certificate, client_key = issue(
        "unnest-runtime",
        ExtendedKeyUsageOID.CLIENT_AUTH,
        server=False,
    )
    ca_blob = ca_certificate.public_bytes(serialization.Encoding.PEM)
    materials = {
        runtime_directory / "ca.crt": (ca_blob, 0o644),
        runtime_directory / "client.crt": (client_certificate, 0o644),
        runtime_directory / "client.key": (client_key, 0o640),
        worker_directory / "ca.crt": (ca_blob, 0o644),
        worker_directory / "server.crt": (server_certificate, 0o644),
        worker_directory / "server.key": (server_key, 0o640),
    }
    for path, (contents, mode) in materials.items():
        path.write_bytes(contents)
        if os.geteuid() == 0:
            os.chown(path, 0, 0)
        path.chmod(mode)
    if os.geteuid() == 0:
        os.chown(runtime_directory, 0, 0)
        os.chown(worker_directory, 0, 0)
    runtime_directory.chmod(0o750)
    worker_directory.chmod(0o750)


def _prepare_install_directory(
    package: Path,
    manifest: dict[str, Any],
    *,
    install_root: Path,
    server_name: str,
    certificate: Path | None,
    private_key: Path | None,
) -> Path:
    if install_root.is_symlink():
        raise PackageValidationError("Install root must not be a symbolic link")
    install_root = install_root.resolve()
    release_version = manifest.get("release_version")
    if not isinstance(release_version, str) or not _SEMVER_RE.fullmatch(release_version):
        raise PackageValidationError("Release version is not a valid SemVer value")
    install_root.mkdir(parents=True, exist_ok=True)
    if (install_root / "current.json").exists():
        raise PackageValidationError("An Unnest release is already installed; automatic upgrade is not supported")
    release_directory = install_root / "releases" / release_version
    releases = release_directory.parent
    releases.mkdir(parents=True, exist_ok=True)
    if release_directory.exists():
        try:
            installed_manifest = json.loads(
                (release_directory / "manifest" / "release.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PackageValidationError("Existing release install directory is incomplete") from exc
        if installed_manifest.get("release_digest") != manifest.get("release_digest"):
            raise PackageValidationError("Existing release install directory contains a different release")
        if not all(
            (release_directory / relative).is_file()
            for relative in (
                ".env",
                "compose.yml",
                "tls/server.crt",
                "tls/server.key",
                "sandbox-tls/runtime/ca.crt",
                "sandbox-tls/runtime/client.crt",
                "sandbox-tls/runtime/client.key",
                "sandbox-tls/worker/ca.crt",
                "sandbox-tls/worker/server.crt",
                "sandbox-tls/worker/server.key",
            )
        ):
            raise PackageValidationError("Existing release install directory is incomplete")
        return release_directory
    with TemporaryDirectory(prefix=f".{release_version}-", dir=releases) as staging:
        staging_directory = Path(staging)
        for source_relative, destination_relative in (
            ("compose/compose.yml", "compose.yml"),
            ("manifest/release.json", "manifest/release.json"),
            ("openapi/openapi.json", "openapi/openapi.json"),
            ("tests/acceptance.json", "tests/acceptance.json"),
            ("license/license.json", "license/license.json"),
            ("license/license.sig", "license/license.sig"),
        ):
            destination = staging_directory / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package / source_relative, destination)
        environment = staging_directory / ".env"
        environment.write_text(
            f"UNNEST_DB_PASSWORD={secrets.token_urlsafe(32)}\nUNNEST_INSTALL_GID={os.getegid()}\n",
            encoding="utf-8",
        )
        environment.chmod(0o600)
        _write_tls_material(
            staging_directory / "tls",
            server_name=server_name,
            certificate=certificate,
            private_key=private_key,
        )
        _write_sandbox_mtls_material(staging_directory / "sandbox-tls")
        staging_directory.replace(release_directory)
    return release_directory


def _installed_release(install_root: Path) -> tuple[Path, dict[str, Any], str]:
    if install_root.is_symlink():
        raise PackageValidationError("Install root must not be a symbolic link")
    root = install_root.resolve()
    marker_path = root / "current.json"
    if not marker_path.is_file() or marker_path.is_symlink():
        raise PackageValidationError("Unnest is not installed")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageValidationError("Installed release marker is invalid") from exc
    if not isinstance(marker, dict):
        raise PackageValidationError("Installed release marker is invalid")
    raw_directory = marker.get("directory")
    if not isinstance(raw_directory, str) or not Path(raw_directory).is_absolute():
        raise PackageValidationError("Installed release directory is invalid")
    configured_directory = Path(raw_directory)
    if configured_directory.is_symlink():
        raise PackageValidationError("Installed release directory is invalid")
    release_directory = configured_directory.resolve()
    if root not in release_directory.parents or not release_directory.is_dir():
        raise PackageValidationError("Installed release directory is invalid")
    manifest = load_manifest(release_directory)
    if marker.get("release_version") != manifest.get("release_version") or marker.get("release_digest") != manifest.get(
        "release_digest"
    ):
        raise PackageValidationError("Installed release marker does not match its manifest")
    raw_url = marker.get("url")
    parsed_url = urlparse(raw_url) if isinstance(raw_url, str) else None
    if (
        parsed_url is None
        or parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
        or parsed_url.path != "/setup"
    ):
        raise PackageValidationError("Installed Runtime URL is invalid")
    for relative in (".env", "compose.yml", "tls/server.crt"):
        path = release_directory / relative
        if not path.is_file() or path.is_symlink():
            raise PackageValidationError("Installed release is incomplete")
    return release_directory, manifest, f"{parsed_url.scheme}://{parsed_url.netloc}"


def _run_capture(command: list[str], cwd: Path) -> str:
    import subprocess

    completed = subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise PackageValidationError(f"Command failed with exit code {completed.returncode}: {command[0]}")
    return completed.stdout


def inspect_installation(
    install_root: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Inspect Compose processes and the installed Runtime health endpoints."""
    release_directory, manifest, base_url = _installed_release(install_root)
    running_output = _run_capture(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "compose.yml",
            "ps",
            "--services",
            "--status",
            "running",
        ],
        release_directory,
    )
    running = sorted({line.strip() for line in running_output.splitlines() if line.strip()})
    expected = sorted(service for service in manifest.get("services", []) if isinstance(service, str))
    missing = sorted(set(expected).difference(running))
    unexpected = sorted(set(running).difference(expected))
    verify: bool | str = str(release_directory / "tls" / "server.crt") if transport is None else False
    endpoint_status: dict[str, int | None] = {}
    with httpx.Client(base_url=base_url, verify=verify, transport=transport, timeout=5, trust_env=False) as client:
        for path in ("/health", "/ready"):
            try:
                endpoint_status[path] = client.get(path).status_code
            except httpx.HTTPError:
                endpoint_status[path] = None
    return {
        "release_version": manifest.get("release_version"),
        "release_digest": manifest.get("release_digest"),
        "base_url": base_url,
        "running_services": running,
        "missing_services": missing,
        "unexpected_services": unexpected,
        "health_status": endpoint_status["/health"],
        "ready_status": endpoint_status["/ready"],
        "healthy": not missing and not unexpected and endpoint_status["/health"] == HTTPStatus.OK,
        "ready": not missing and not unexpected and endpoint_status["/ready"] == HTTPStatus.OK,
    }


def download_installed_backup(
    install_root: Path,
    *,
    admin_username: str,
    admin_password: str,
    output_directory: Path,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Create an encrypted Runtime backup through the admin API and download it."""
    release_directory, _manifest, base_url = _installed_release(install_root)
    if output_directory.is_symlink():
        raise PackageValidationError("Backup output directory must not be a symbolic link")
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    verify: bool | str = str(release_directory / "tls" / "server.crt") if transport is None else False
    try:
        with httpx.Client(
            base_url=base_url,
            verify=verify,
            transport=transport,
            timeout=120,
            trust_env=False,
        ) as client:
            login = client.post(
                "/api/v1/login",
                data={"username": admin_username, "password": admin_password},
            )
            login.raise_for_status()
            login_payload = login.json()
            access_token = login_payload.get("access_token") if isinstance(login_payload, dict) else None
            if not isinstance(access_token, str) or not access_token:
                raise PackageValidationError("Runtime login returned an invalid token")
            headers = {"Authorization": f"Bearer {access_token}"}
            created = client.post("/api/v1/admin/backups", headers=headers)
            created.raise_for_status()
            backup = created.json()
            if not isinstance(backup, dict):
                raise PackageValidationError("Runtime backup response is invalid")
            backup_id = backup.get("id")
            checksum = backup.get("checksum")
            size_bytes = backup.get("size_bytes")
            if (
                not isinstance(backup_id, str)
                or not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes <= 0
            ):
                raise PackageValidationError("Runtime backup response is invalid")
            try:
                normalized_id = str(UUID(backup_id))
            except ValueError as exc:
                raise PackageValidationError("Runtime backup identifier is invalid") from exc
            destination = output_directory.resolve() / f"{normalized_id}.unnest-backup"
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            digest = hashlib.sha256()
            received = 0
            try:
                with (
                    os.fdopen(descriptor, "wb") as output,
                    client.stream(
                        "GET",
                        f"/api/v1/admin/backups/{normalized_id}/download",
                        headers=headers,
                    ) as response,
                ):
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        received += len(chunk)
                        if received > size_bytes:
                            raise PackageValidationError("Runtime backup download is larger than declared")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except Exception:
                destination.unlink(missing_ok=True)
                raise
    except httpx.HTTPError as exc:
        raise PackageValidationError("Runtime backup API request failed") from exc
    if received != size_bytes or digest.hexdigest() != checksum:
        destination.unlink(missing_ok=True)
        raise PackageValidationError("Runtime backup download failed integrity verification")
    return destination


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
def install(
    package: Path = typer.Argument(..., exists=True),
    install_root: Path = typer.Option(Path("/opt/unnest"), "--install-root"),
    server_name: str = typer.Option("", "--server-name"),
    tls_certificate: Path | None = typer.Option(None, "--tls-cert", exists=True, dir_okay=False),
    tls_private_key: Path | None = typer.Option(None, "--tls-key", exists=True, dir_okay=False),
) -> None:
    resolved_server_name = server_name or _default_server_name()
    with _materialize_package(package) as root:
        manifest = _verify_package_directory(root)
        _preflight_directory(root)
        if manifest.get("deployment", {}).get("tls") == "institution" and (
            tls_certificate is None or tls_private_key is None
        ):
            raise PackageValidationError("This release requires an institution TLS certificate and private key")
        release_directory = _prepare_install_directory(
            root,
            manifest,
            install_root=install_root,
            server_name=resolved_server_name,
            certificate=tls_certificate,
            private_key=tls_private_key,
        )
        for image in sorted((root / "images").glob("*.tar")):
            _run(["docker", "image", "load", "--input", str(image)], root)
        _run(
            [
                "docker",
                "compose",
                "--env-file",
                ".env",
                "-f",
                "compose.yml",
                "up",
                "-d",
                "--pull",
                "never",
            ],
            release_directory,
        )
        marker = install_root.resolve() / "current.json"
        temporary_marker = marker.with_suffix(".tmp")
        temporary_marker.write_text(
            json.dumps(
                {
                    "release_version": manifest["release_version"],
                    "release_digest": manifest["release_digest"],
                    "directory": str(release_directory),
                    "url": f"https://{resolved_server_name}:7860/setup",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary_marker.replace(marker)
    typer.echo(f"installed; open https://{resolved_server_name}:7860/setup to complete initial setup")


@app.command()
def status(
    install_root: Path = typer.Option(Path("/opt/unnest"), "--install-root"),
) -> None:
    result = inspect_installation(install_root)
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["healthy"]:
        raise typer.Exit(code=1)


@app.command()
def backup(
    install_root: Path = typer.Option(Path("/opt/unnest"), "--install-root"),
    admin_username: str = typer.Option(..., "--username", envvar="UNNEST_ADMIN_USERNAME"),
    admin_password: str | None = typer.Option(
        None,
        "--admin-password",
        envvar="UNNEST_ADMIN_PASSWORD",
        hidden=True,
    ),
    output_directory: Path = typer.Option(Path(), "--output-dir"),
) -> None:
    password = admin_password or typer.prompt("Admin password", hide_input=True)
    destination = download_installed_backup(
        install_root,
        admin_username=admin_username,
        admin_password=password,
        output_directory=output_directory,
    )
    typer.echo(f"backup saved: {destination}")


def _installed_compose_command(*arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        ".env",
        "-f",
        "compose.yml",
        *arguments,
    ]


def _installed_runtime_gid(release_directory: Path) -> int:
    try:
        value = next(
            line.partition("=")[2]
            for line in (release_directory / ".env").read_text(encoding="utf-8").splitlines()
            if line.startswith("UNNEST_INSTALL_GID=")
        )
        group_id = int(value)
    except (OSError, StopIteration, ValueError) as exc:
        raise PackageValidationError("Installed Runtime group is invalid") from exc
    if group_id < 0:
        raise PackageValidationError("Installed Runtime group is invalid")
    if os.geteuid() != 0 and os.getegid() != group_id:
        raise PackageValidationError("Run restore as root or as the account that installed Unnest")
    return group_id


def _restore_installed_backup(
    install_root: Path,
    *,
    backup: Path,
    identity_file: Path,
) -> None:
    release_directory, manifest, _base_url = _installed_release(install_root)
    if backup.is_symlink() or identity_file.is_symlink():
        raise PackageValidationError("Backup and recovery identity must not be symbolic links")
    backup = backup.resolve()
    identity_file = identity_file.resolve()
    runtime_gid = _installed_runtime_gid(release_directory)
    stopped_services = [
        "runtime",
        "redis",
        "sandbox-controller",
        "sandbox-executor",
        "sandbox-gateway",
        "sandbox-egress-proxy",
    ]
    state_restored = False
    try:
        _run(_installed_compose_command("stop", *stopped_services), release_directory)
        _run(
            _installed_compose_command("up", "-d", "--pull", "never", "postgresql"),
            release_directory,
        )
        with TemporaryDirectory(prefix=".unnest-restore-", dir=release_directory) as temporary:
            restore_input = Path(temporary)
            staged_backup = restore_input / "backup.unnest-backup"
            staged_identity = restore_input / "recovery.txt"
            shutil.copyfile(backup, staged_backup)
            shutil.copyfile(identity_file, staged_identity)
            restore_input.chmod(0o750)
            staged_backup.chmod(0o640)
            staged_identity.chmod(0o640)
            if os.geteuid() == 0:
                os.chown(restore_input, -1, runtime_gid)
                os.chown(staged_backup, -1, runtime_gid)
                os.chown(staged_identity, -1, runtime_gid)
            _run(
                _installed_compose_command(
                    "--profile",
                    "maintenance",
                    "run",
                    "--rm",
                    "--no-deps",
                    "--volume",
                    f"{restore_input}:/restore-input:ro,Z",
                    "restore",
                    "python",
                    "-m",
                    "langflow.unnestctl",
                    "restore",
                    "/restore-input/backup.unnest-backup",
                    "--identity",
                    "/restore-input/recovery.txt",
                    "--storage-dir",
                    "/app/langflow",
                    "--master-key",
                    "/app/langflow/secrets/master.key",
                    "--expected-release",
                    str(manifest["release_version"]),
                    "--allow-group-readable-identity",
                    "--runtime-stopped",
                    "--yes",
                ),
                release_directory,
            )
            state_restored = True
        _run(
            _installed_compose_command("up", "-d", "--pull", "never", "redis"),
            release_directory,
        )
        _run(
            _installed_compose_command("exec", "-T", "redis", "redis-cli", "FLUSHALL"),
            release_directory,
        )
    except (OSError, PackageValidationError) as restore_error:
        if state_restored:
            raise PackageValidationError(
                "Runtime state was restored but Redis could not be reset; the application remains stopped"
            ) from restore_error
        try:
            _run(
                _installed_compose_command("up", "-d", "--pull", "never"),
                release_directory,
            )
        except PackageValidationError as restart_error:
            raise PackageValidationError(
                "Restore failed and the previous Runtime stack could not be restarted"
            ) from restart_error
        if isinstance(restore_error, PackageValidationError):
            raise
        raise PackageValidationError("Restore failed while staging the encrypted backup") from restore_error
    _run(
        _installed_compose_command("up", "-d", "--pull", "never"),
        release_directory,
    )


@app.command()
def restore(
    backup: Path = typer.Argument(..., exists=True, dir_okay=False),
    identity_file: Path = typer.Option(..., "--identity", exists=True, dir_okay=False),
    install_root: Path = typer.Option(Path("/opt/unnest"), "--install-root"),
    database_url: str | None = typer.Option(None, envvar="LANGFLOW_DATABASE_URL"),
    storage_directory: Path = typer.Option(Path("/opt/unnest/data"), "--storage-dir"),
    master_key: Path = typer.Option(Path("/opt/unnest/secrets/master.key"), "--master-key"),
    license_directory: Path | None = typer.Option(None, "--license-dir"),
    key_directory: Path | None = typer.Option(None, "--key-dir"),
    expected_release_version: str | None = typer.Option(None, "--expected-release", hidden=True),
    allow_group_readable_identity: bool = typer.Option(  # noqa: FBT001
        False,  # noqa: FBT003
        "--allow-group-readable-identity",
        hidden=True,
    ),
    runtime_stopped: bool = typer.Option(False, "--runtime-stopped"),  # noqa: FBT001, FBT003
    yes: bool = typer.Option(False, "--yes"),  # noqa: FBT001, FBT003
) -> None:
    if platform.system() != "Linux":
        raise PackageValidationError("Only Linux is supported")
    if backup.is_symlink() or identity_file.is_symlink():
        raise PackageValidationError("Backup and recovery identity must not be symbolic links")
    if database_url is not None and not runtime_stopped:
        raise PackageValidationError(
            "Stop every Runtime, scheduler, and worker instance before restore, then pass --runtime-stopped"
        )
    identity_stat = identity_file.stat()
    identity_mode = stat.S_IMODE(identity_stat.st_mode)
    internal_group_copy = (
        allow_group_readable_identity
        and identity_mode == IDENTITY_GROUP_READABLE_MODE
        and identity_stat.st_gid in {os.getegid(), *os.getgroups()}
    )
    if identity_mode != IDENTITY_PRIVATE_MODE and not internal_group_copy:
        raise PackageValidationError("Recovery identity file must have mode 0600")
    if not yes and not typer.confirm("Restore will replace runtime database, files, and keys. Continue?"):
        raise typer.Abort
    if database_url is None:
        _restore_installed_backup(
            install_root,
            backup=backup,
            identity_file=identity_file,
        )
        typer.echo("restored installed Runtime; wait for /ready and run unnestctl acceptance")
        return
    try:
        result = restore_runtime_backup(
            path=backup,
            identity=identity_file.read_text(encoding="utf-8").strip(),
            database_url=database_url,
            storage_directory=storage_directory,
            master_key_destination=master_key,
            license_directory=license_directory,
            key_directory=key_directory,
            expected_release_version=expected_release_version,
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
