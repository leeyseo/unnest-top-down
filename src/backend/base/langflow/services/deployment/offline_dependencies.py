# ruff: noqa: EM101, EM102, TRY003
"""Resolve and verify hash-locked Python wheels for offline releases."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from packaging.tags import Tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from langflow.services.deployment.offline_package import sha256_file

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KINDS = ("python_packages", "os_packages", "binaries")
_MAX_REQUIREMENTS_SIZE = 16 * 1024**2


class DependencyLockError(ValueError):
    """Raised when immutable dependency declarations do not resolve exactly."""


def validated_dependency_lock(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    expected = {kind: {} for kind in _KINDS}
    entries = manifest.get("flows")
    if not isinstance(entries, list):
        raise DependencyLockError("Release manifest does not declare flows")
    for entry in entries:
        declarations = entry.get("declared_dependencies", {}) if isinstance(entry, dict) else {}
        if not isinstance(declarations, dict):
            raise DependencyLockError("Flow dependency declaration is invalid")
        for kind in _KINDS:
            items = declarations.get(kind, [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise DependencyLockError(f"Flow {kind} dependency declaration is invalid")
            for item in items:
                name = item.get("name")
                version = item.get("version")
                if not isinstance(name, str) or not isinstance(version, str):
                    raise DependencyLockError(f"Flow {kind} dependency identity is invalid")
                key = name.casefold()
                current = expected[kind].get(key)
                if current is not None and current != item:
                    raise DependencyLockError(f"Conflicting Flow dependency declaration: {name}")
                expected[kind][key] = item
    normalized = {
        kind: sorted(values.values(), key=lambda item: (item["name"].casefold(), item["version"]))
        for kind, values in expected.items()
    }
    if manifest.get("dependency_lock") != normalized:
        raise DependencyLockError("Release dependency lock does not match the immutable Flow declarations")
    return normalized


def _locked_requirement(item: dict[str, Any]) -> tuple[str, str, set[str], str]:
    name, version, hashes = item.get("name"), item.get("version"), item.get("hashes")
    if (
        not isinstance(name, str)
        or not isinstance(version, str)
        or not isinstance(hashes, list)
        or not hashes
        or any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in hashes)
    ):
        raise DependencyLockError("Python dependency lock entry is invalid")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise DependencyLockError(f"Python dependency version is invalid: {name}") from exc
    expected_hashes = set(hashes)
    line = f"{name}=={version} " + " ".join(f"--hash={value}" for value in sorted(expected_hashes)) + "\n"
    return name, version, expected_hashes, line


def stage_locked_wheels(
    wheelhouse: Path | None,
    destination: Path,
    manifest: dict[str, Any],
) -> None:
    """Copy the complete immutable wheel lock from an approved local directory."""
    dependencies = validated_dependency_lock(manifest)["python_packages"]
    if dependencies and wheelhouse is None:
        raise DependencyLockError("Python dependencies require UNNEST_OFFLINE_WHEELHOUSE")
    destination.mkdir()

    available: list[tuple[Path, str, Version, str, frozenset[Tag]]] = []
    for wheel in sorted(wheelhouse.glob("*.whl")) if dependencies and wheelhouse else []:
        if not wheel.is_file() or wheel.is_symlink():
            raise DependencyLockError(f"Offline wheelhouse contains an invalid wheel: {wheel.name}")
        try:
            name, version, _build, tags = parse_wheel_filename(wheel.name)
        except (InvalidVersion, ValueError) as exc:
            raise DependencyLockError(f"Offline wheelhouse contains an invalid wheel filename: {wheel.name}") from exc
        available.append((wheel, canonicalize_name(name), version, f"sha256:{sha256_file(wheel)}", tags))

    resolved: list[dict[str, str]] = []
    requirements: list[str] = []
    compatible_tags = set(sys_tags())
    for dependency in dependencies:
        name, version, expected_hashes, requirement = _locked_requirement(dependency)
        expected_version = Version(version)
        matches = [
            (wheel, digest, tags)
            for wheel, normalized_name, wheel_version, digest, tags in available
            if normalized_name == canonicalize_name(name)
            and wheel_version == expected_version
            and digest in expected_hashes
        ]
        if {digest for _wheel, digest, _tags in matches} != expected_hashes:
            raise DependencyLockError(
                f"Approved wheelhouse does not satisfy the complete lock for {name}=={version}"
            )
        if not any(tags.intersection(compatible_tags) for _wheel, _digest, tags in matches):
            raise DependencyLockError(f"No locked wheel is compatible with Linux AMD64 for {name}=={version}")
        for wheel, digest, _tags in matches:
            target = destination / wheel.name
            if target.exists() and f"sha256:{sha256_file(target)}" != digest:
                raise DependencyLockError(f"Conflicting locked wheel filename: {wheel.name}")
            if not target.exists():
                shutil.copyfile(wheel, target)
                if f"sha256:{sha256_file(target)}" != digest:
                    target.unlink(missing_ok=True)
                    raise DependencyLockError(f"Locked wheel changed while being staged: {wheel.name}")
            resolved.append({"name": name, "version": version, "file": wheel.name, "digest": digest})
        requirements.append(requirement)

    (destination / "requirements.lock").write_text("".join(requirements), encoding="utf-8", newline="\n")
    manifest["build"]["dependency_lock_status"] = "resolved"
    manifest["build"]["resolved_wheels"] = sorted(
        resolved,
        key=lambda item: (item["name"].casefold(), item["version"], item["file"]),
    )


def verify_locked_wheels(package: Path, manifest: dict[str, Any]) -> None:
    """Verify package wheels against immutable Flow declarations and the resolved manifest."""
    dependencies = validated_dependency_lock(manifest)["python_packages"]
    resolved = manifest.get("build", {}).get("resolved_wheels", [])
    if manifest.get("build", {}).get("dependency_lock_status") != "resolved" or not isinstance(resolved, list):
        raise DependencyLockError("Python dependency lock is invalid")

    declared: set[tuple[str, str, str]] = set()
    requirements: list[str] = []
    for dependency in dependencies:
        name, version, expected_hashes, requirement = _locked_requirement(dependency)
        declared.update((canonicalize_name(name), version, digest) for digest in expected_hashes)
        requirements.append(requirement)

    expected_wheels: dict[str, tuple[str, str, str]] = {}
    for item in resolved:
        if not isinstance(item, dict):
            raise DependencyLockError("Resolved wheel entry is invalid")
        name, version, filename, digest = (
            item.get("name"),
            item.get("version"),
            item.get("file"),
            item.get("digest"),
        )
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(filename, str)
            or not isinstance(digest, str)
            or Path(filename).name != filename
            or not filename.endswith(".whl")
            or not _SHA256_RE.fullmatch(digest)
            or filename in expected_wheels
        ):
            raise DependencyLockError("Resolved wheel entry is invalid")
        expected_wheels[filename] = (name, version, digest)

    actual: set[tuple[str, str, str]] = set()
    wheel_root = package / "wheels"
    for filename, (name, version, digest) in expected_wheels.items():
        wheel = wheel_root / filename
        try:
            wheel_name, wheel_version, _build, _tags = parse_wheel_filename(filename)
            locked_version = Version(version)
        except (InvalidVersion, ValueError) as exc:
            raise DependencyLockError(f"Resolved wheel filename is invalid: {filename}") from exc
        if (
            wheel.is_symlink()
            or not wheel.is_file()
            or package.resolve() not in wheel.resolve().parents
            or canonicalize_name(wheel_name) != canonicalize_name(name)
            or wheel_version != locked_version
            or f"sha256:{sha256_file(wheel)}" != digest
        ):
            raise DependencyLockError(f"Resolved wheel does not match its lock: {filename}")
        actual.add((canonicalize_name(name), version, digest))
    if actual != declared:
        raise DependencyLockError("Resolved wheels do not exactly satisfy the Python dependency lock")
    if {path.name for path in wheel_root.glob("*.whl")} != set(expected_wheels):
        raise DependencyLockError("Bundled wheel files do not exactly match the release manifest")

    lock_path = wheel_root / "requirements.lock"
    try:
        if lock_path.is_symlink() or lock_path.stat().st_size > _MAX_REQUIREMENTS_SIZE:
            raise DependencyLockError("Python requirements lock is invalid")
        lock = lock_path.read_bytes()
    except OSError as exc:
        raise DependencyLockError("Python requirements lock is missing") from exc
    if lock != "".join(requirements).encode():
        raise DependencyLockError("Python requirements lock does not match the release manifest")
