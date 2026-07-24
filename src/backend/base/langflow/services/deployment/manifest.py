"""Static analysis and deterministic manifest generation for on-prem exports."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import warnings as python_warnings
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from lfx.utils.flow_requirements import generate_requirements_from_flow
from sqlalchemy import or_
from sqlmodel import col, select

from langflow.agentic.helpers.code_security import scan_code_security
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from langflow.api.v1.schemas.on_prem_deployments import (
        AcceptanceTestCreate,
        AgentApiContract,
        OnPremDeploymentConfig,
    )

_SUBFLOW_TYPES = {"FlowTool", "RunFlow"}
_SERVICE_NAMES = {
    "postgres": "postgresql",
    "redis": "redis",
    "minio": "minio",
    "s3": "s3",
    "qdrant": "qdrant",
    "chroma": "chroma",
    "pgvector": "pgvector",
    "clamav": "clamav",
    "prometheus": "prometheus",
    "grafana": "grafana",
    "ollama": "ollama",
}
_MODEL_FIELDS = {"model", "model_id", "model_name", "llm_model", "embedding_model"}
_KB_FIELDS = {"knowledge_base", "knowledge_base_name", "kb_name", "collection_name"}
_RISKY_NODE_MARKERS = ("customcomponent", "pythonrepl", "pythoninterpreter", "shell", "subprocess")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEPENDENCY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")
_UNPINNED_VERSION_RE = re.compile(r"[<>=!~*\s]")
_DEPENDENCY_KINDS = ("python_packages", "os_packages", "binaries")
_MIN_HTTP_STATUS = 100
_MAX_HTTP_STATUS = 599
_DIRECT_CONVERSATION_STORE_TYPES = {"messagestore", "storemessage"}
_SERVICE_PORTS = {
    "runtime": [("http", 7860)],
    "postgresql": [("postgresql", 5432)],
    "redis": [("redis", 6379)],
    "minio": [("api", 9000), ("console", 9001)],
    "clamav": [("clamd", 3310)],
    "prometheus": [("http", 9090)],
    "grafana": [("http", 3000)],
    "ollama": [("http", 11434)],
}


@dataclass(frozen=True)
class ReleaseAnalysis:
    manifest: dict[str, Any] | None
    subflow_version_ids: tuple[UUID, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _template(node: dict[str, Any]) -> dict[str, Any]:
    value = node.get("data", {}).get("node", {}).get("template", {})
    return value if isinstance(value, dict) else {}


def _node_type(node: dict[str, Any]) -> str:
    node_data = node.get("data", {})
    node_info = node_data.get("node", {})
    template = node_info.get("template", {})
    for value in (node_data.get("type"), node_info.get("name"), template.get("_type")):
        if isinstance(value, str) and value:
            return value
    return ""


def _field_value(template: dict[str, Any], field_name: str) -> Any:
    field = template.get(field_name)
    return field.get("value") if isinstance(field, dict) else None


def _conversation_persistence_components(data: dict[str, Any]) -> list[str]:
    components: list[str] = []
    for node in data.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_type = _node_type(node)
        normalized_type = node_type.casefold()
        template = _template(node)
        stores_directly = normalized_type in _DIRECT_CONVERSATION_STORE_TYPES or (
            normalized_type == "memory" and str(_field_value(template, "mode") or "Retrieve").casefold() == "store"
        )
        if stores_directly:
            components.append(str(node.get("id") or node_type or "<unknown>"))
    return sorted(set(components))


def _deployment_services(config: OnPremDeploymentConfig, detected: set[str]) -> list[str]:
    services = {*detected, "runtime", "redis"}
    if config.database == "embedded-postgresql":
        services.add("postgresql")
    if config.storage == "minio":
        services.add("minio")
    if config.model_runtime == "bundled":
        services.add("ollama")
    if config.features.clamav:
        services.add("clamav")
    if config.features.grafana:
        services.add("grafana")
    return sorted(services)


def _deployment_ports(services: list[str]) -> list[dict[str, Any]]:
    ports = [
        {
            "service": "runtime",
            "name": "https",
            "port": 7860,
            "protocol": "tcp",
            "scope": "host",
        }
    ]
    ports.extend(
        {
            "service": service,
            "name": name,
            "port": port,
            "protocol": "tcp",
            "scope": "internal",
        }
        for service in services
        for name, port in _SERVICE_PORTS.get(service, [])
    )
    return ports


def _validate_json_schema(schema: dict[str, Any], name: str) -> list[str]:
    if schema.get("type") != "object":
        return [f"{name} must be a JSON Schema object"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"{name} is invalid: {exc.message}"]
    return []


def _example_errors(schema: dict[str, Any], example: Any, name: str) -> list[str]:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return []
    errors = sorted(Draft202012Validator(schema).iter_errors(example), key=lambda error: str(list(error.path)))
    return [
        f"{name} does not match its schema at {'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors[:10]
    ]


def _contains_plaintext_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(
                marker in normalized for marker in ("password", "secret", "api_key", "token", "authorization")
            ) and nested not in (None, "", "***"):
                return True
            if _contains_plaintext_secret(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_plaintext_secret(item) for item in value)
    return False


def _safe_endpoint(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse((parsed.scheme, f"{parsed.hostname}{port}", parsed.path or "/", "", "", ""))


def _declared_dependencies(
    deployment_metadata: dict[str, Any],
    *,
    flow_version_id: UUID,
    component: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    dependencies = {kind: [] for kind in _DEPENDENCY_KINDS}
    errors: list[str] = []
    prefix = f"Flow version {flow_version_id} component '{component}'"
    for kind in _DEPENDENCY_KINDS:
        declarations = deployment_metadata.get(kind, [])
        if not isinstance(declarations, list):
            errors.append(f"{prefix} deployment.{kind} must be a list")
            continue
        for index, declaration in enumerate(declarations):
            label = f"{prefix} deployment.{kind}[{index}]"
            if not isinstance(declaration, dict):
                errors.append(f"{label} must be an object")
                continue
            name = declaration.get("name")
            version = declaration.get("version")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{label}.name is required")
            elif not _DEPENDENCY_NAME_RE.fullmatch(name.strip()):
                errors.append(f"{label}.name contains unsupported characters")
            if not isinstance(version, str) or not version.strip():
                errors.append(f"{label}.version is required")
            elif _UNPINNED_VERSION_RE.search(version.strip()):
                errors.append(f"{label}.version must be an exact version, not a range")
            if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
                continue
            normalized: dict[str, Any] = {
                "name": name.strip(),
                "version": version.strip(),
            }
            if kind == "python_packages":
                hashes = declaration.get("hashes")
                if (
                    not isinstance(hashes, list)
                    or not hashes
                    or any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in hashes)
                ):
                    errors.append(f"{label}.hashes must contain one or more lowercase SHA-256 digests")
                    continue
                normalized["hashes"] = sorted(set(hashes))
            else:
                checksum = declaration.get("checksum")
                if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
                    errors.append(f"{label}.checksum must be a lowercase SHA-256 digest")
                    continue
                normalized["checksum"] = checksum
            if kind == "binaries":
                source = declaration.get("source")
                license_name = declaration.get("license")
                parsed_source = _safe_endpoint(source) if isinstance(source, str) else None
                if parsed_source is None or not parsed_source.startswith("https://") or parsed_source != source:
                    errors.append(
                        f"{label}.source must be an HTTPS URL without credentials, query parameters, or fragments"
                    )
                    continue
                if not isinstance(license_name, str) or not license_name.strip():
                    errors.append(f"{label}.license is required")
                    continue
                normalized.update(source=source, license=license_name.strip())
            dependencies[kind].append(normalized)
    return dependencies, errors


def _merge_declared_dependencies(
    target: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
) -> list[str]:
    errors: list[str] = []
    for kind in _DEPENDENCY_KINDS:
        by_name = {item["name"].casefold(): item for item in target[kind]}
        for item in incoming[kind]:
            key = item["name"].casefold()
            existing = by_name.get(key)
            if existing is not None and existing != item:
                errors.append(
                    f"Conflicting {kind} lock declarations for '{item['name']}': "
                    f"{existing['version']} and {item['version']}"
                )
                continue
            if existing is None:
                target[kind].append(item)
                by_name[key] = item
        target[kind].sort(key=lambda value: (value["name"].casefold(), value["version"]))
    return errors


def _is_breaking(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_properties = previous.get("properties", {})
    current_properties = current.get("properties", {})
    if not isinstance(previous_properties, dict) or not isinstance(current_properties, dict):
        return previous != current
    if set(previous_properties).difference(current_properties):
        return True
    for name in set(previous_properties).intersection(current_properties):
        old_type = previous_properties[name].get("type") if isinstance(previous_properties[name], dict) else None
        new_type = current_properties[name].get("type") if isinstance(current_properties[name], dict) else None
        if old_type != new_type:
            return True
    old_required = set(previous.get("required", []))
    new_required = set(current.get("required", []))
    return bool(new_required.difference(old_required))


def next_api_version(
    previous_manifest: dict[str, Any] | None,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None = None,
) -> str:
    if not previous_manifest:
        return "v1"
    current = str(previous_manifest.get("api", {}).get("version", "v1"))
    previous_api = previous_manifest.get("api", {})
    input_breaks = _is_breaking(previous_api.get("input_schema", {}), input_schema)
    output_breaks = output_schema is not None and _is_breaking(
        previous_api.get("output_schema", {}),
        output_schema,
    )
    if not input_breaks and not output_breaks:
        return current
    try:
        return f"v{int(current.removeprefix('v')) + 1}"
    except ValueError:
        return "v2"


def build_openapi(
    *,
    release_version: str,
    api_version: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Unnest Agent API", "version": release_version},
        "paths": {
            f"/api/{api_version}/agent/run": {
                "post": {
                    "operationId": "runAgent",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": input_schema}},
                    },
                    "responses": {
                        "200": {
                            "description": "Agent result",
                            "content": {"application/json": {"schema": output_schema}},
                        }
                    },
                }
            }
        },
    }


def _inspect_flow(
    version: FlowVersion,
) -> tuple[
    dict[str, Any],
    list[str],
    list[str],
    set[str],
    set[str],
    set[str],
    set[str],
    bool,
    dict[str, list[dict[str, Any]]],
]:
    declared_dependencies = {kind: [] for kind in _DEPENDENCY_KINDS}
    data = version.data
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        return (
            {},
            [f"Flow version {version.id} does not contain a valid nodes/edges graph"],
            [],
            set(),
            set(),
            set(),
            set(),
            False,
            declared_dependencies,
        )

    errors: list[str] = []
    warnings: list[str] = []
    secrets: set[str] = set()
    endpoints: set[str] = set()
    services: set[str] = set()
    models: set[str] = set()
    allowed_sandbox_endpoints: set[str] = set()
    sandbox = False

    for node in data["nodes"]:
        if not isinstance(node, dict):
            continue
        node_type = _node_type(node)
        lowered_type = node_type.lower()
        sandbox = sandbox or any(marker in lowered_type for marker in _RISKY_NODE_MARKERS)
        for marker, service in _SERVICE_NAMES.items():
            if marker in lowered_type:
                services.add(service)

        template = _template(node)
        for field_name, field in template.items():
            if not isinstance(field, dict):
                continue
            value = field.get("value")
            if field.get("password") is True and value not in (None, ""):
                if field.get("load_from_db") is True and isinstance(value, str):
                    secrets.add(value.strip())
                else:
                    errors.append(
                        f"Flow version {version.id} has a plaintext value in password field '{field_name}'; "
                        "replace it with a global variable reference"
                    )
            elif field.get("load_from_db") is True and isinstance(value, str) and value.strip():
                secrets.add(value.strip())

            if field_name in _MODEL_FIELDS and isinstance(value, str) and value.strip():
                models.add(value.strip())
            if isinstance(value, str) and (endpoint := _safe_endpoint(value)):
                endpoints.add(endpoint)

        node_metadata = node.get("data", {}).get("node", {}).get("metadata", {})
        deployment_metadata = node_metadata.get("deployment", {}) if isinstance(node_metadata, dict) else {}
        if isinstance(deployment_metadata, dict):
            sandbox = sandbox or deployment_metadata.get("sandbox") is True
            for endpoint in deployment_metadata.get("internal_endpoints", []):
                if isinstance(endpoint, str) and (safe_endpoint := _safe_endpoint(endpoint)):
                    endpoints.add(safe_endpoint)
                    allowed_sandbox_endpoints.add(safe_endpoint)
            declared, dependency_errors = _declared_dependencies(
                deployment_metadata,
                flow_version_id=version.id,
                component=node_type or str(node.get("id") or "<unknown>"),
            )
            errors.extend(dependency_errors)
            errors.extend(_merge_declared_dependencies(declared_dependencies, declared))

        if isinstance(node_metadata, dict) and str(node_metadata.get("module", "")).startswith("custom_components."):
            sandbox = True
            code = _field_value(template, "code")
            if isinstance(code, str):
                result = scan_code_security(code)
                warnings.extend(f"{node_type or 'Custom component'}: {item}" for item in result.violations)

    with python_warnings.catch_warnings(record=True) as caught:
        python_warnings.simplefilter("always")
        dependencies = generate_requirements_from_flow(
            {"data": data},
            include_lfx=True,
            pin_versions=True,
        )
    warnings.extend(str(item.message) for item in caught)
    unpinned = [dependency for dependency in dependencies if "==" not in dependency]
    if unpinned:
        warnings.append(f"Build must resolve and hash unpinned Python dependencies: {', '.join(unpinned)}")

    return (
        {
            "id": str(version.id),
            "flow_id": str(version.flow_id),
            "version_number": version.version_number,
            "digest": canonical_digest(data),
            "dependencies": dependencies,
            "models": sorted(models),
        },
        errors,
        warnings,
        secrets,
        endpoints,
        services,
        allowed_sandbox_endpoints,
        sandbox,
        declared_dependencies,
    )


def _subflow_references(data: dict[str, Any]) -> list[tuple[UUID | None, str | None]]:
    references: list[tuple[UUID | None, str | None]] = []
    for node in data.get("nodes", []):
        if not isinstance(node, dict) or _node_type(node) not in _SUBFLOW_TYPES:
            continue
        template = _template(node)
        raw_id = _field_value(template, "flow_id_selected")
        raw_name = _field_value(template, "flow_name_selected") or _field_value(template, "flow_name")
        try:
            flow_id = UUID(raw_id) if raw_id else None
        except (TypeError, ValueError):
            flow_id = None
        references.append((flow_id, raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None))
    return references


async def _resolve_subflows(
    session: AsyncSession,
    *,
    user_id: UUID,
    roots: list[FlowVersion],
) -> tuple[list[FlowVersion], list[str]]:
    resolved: list[FlowVersion] = []
    errors: list[str] = []
    visited_flow_ids = {root.flow_id for root in roots}
    pending = list(roots)
    while pending:
        parent = pending.pop()
        if not isinstance(parent.data, dict):
            continue
        for flow_id, flow_name in _subflow_references(parent.data):
            if flow_id in visited_flow_ids:
                continue
            condition = Flow.id == flow_id if flow_id else Flow.name == flow_name
            flow = (
                await session.exec(
                    select(Flow).where(Flow.user_id == user_id, condition).order_by(col(Flow.updated_at).desc())
                )
            ).first()
            if flow is None:
                reference = str(flow_id or flow_name or "<empty>")
                errors.append(f"Subflow '{reference}' referenced by flow version {parent.id} was not found")
                continue
            version = (
                await session.exec(
                    select(FlowVersion)
                    .where(FlowVersion.user_id == user_id, FlowVersion.flow_id == flow.id)
                    .order_by(col(FlowVersion.version_number).desc())
                )
            ).first()
            if version is None:
                errors.append(f"Subflow '{flow.name}' has no saved Flow Version")
                continue
            visited_flow_ids.add(flow.id)
            resolved.append(version)
            pending.append(version)
    return resolved, errors


def _knowledge_aliases(data: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for node in data.get("nodes", []):
        if not isinstance(node, dict):
            continue
        template = _template(node)
        for field_name in _KB_FIELDS:
            value = _field_value(template, field_name)
            if isinstance(value, str) and value.strip():
                aliases.add(value.strip())
    return aliases


def _validate_ingestion_contract(agent_data: dict[str, Any], ingestion_data: dict[str, Any]) -> list[str]:
    file_inputs = [
        node
        for node in ingestion_data.get("nodes", [])
        if isinstance(node, dict) and _node_type(node) == "DeploymentFileInput"
    ]
    if len(file_inputs) != 1:
        return ["Ingestion Flow must contain exactly one Deployment File Input component"]

    source_id = file_inputs[0].get("id")
    target_ids = {
        edge.get("target")
        for edge in ingestion_data.get("edges", [])
        if isinstance(edge, dict) and edge.get("source") == source_id
    }
    target_types = {
        _node_type(node).lower()
        for node in ingestion_data.get("nodes", [])
        if isinstance(node, dict) and node.get("id") in target_ids
    }
    errors = []
    if not any("knowledge" in value or "ingest" in value for value in target_types):
        errors.append("Deployment File Input must connect to a Knowledge/Ingest component")

    agent_aliases = _knowledge_aliases(agent_data)
    ingestion_aliases = _knowledge_aliases(ingestion_data)
    if len(agent_aliases) != 1 or agent_aliases != ingestion_aliases:
        errors.append("Agent and Ingestion flows must reference exactly one shared Knowledge Base alias")
    return errors


def _validate_api_mappings(agent_data: dict[str, Any], api: AgentApiContract) -> list[str]:
    nodes = {
        node["id"]: node
        for node in agent_data.get("nodes", [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    input_properties = set(api.input_schema.get("properties", {}))
    output_properties = set(api.output_schema.get("properties", {}))
    required_inputs = set(api.input_schema.get("required", []))
    required_outputs = set(api.output_schema.get("required", []))
    errors: list[str] = []

    if missing := sorted(required_inputs.difference(api.input_mapping)):
        errors.append(f"Required API input fields are not mapped: {', '.join(missing)}")
    if missing := sorted(required_outputs.difference(api.output_mapping)):
        errors.append(f"Required API output fields are not mapped: {', '.join(missing)}")
    if unknown := sorted(set(api.input_mapping).difference(input_properties)):
        errors.append(f"Input mappings refer to unknown schema fields: {', '.join(unknown)}")
    if unknown := sorted(set(api.output_mapping).difference(output_properties)):
        errors.append(f"Output mappings refer to unknown schema fields: {', '.join(unknown)}")

    for field_name, binding in api.input_mapping.items():
        node = nodes.get(binding.component_id)
        if node is None:
            errors.append(f"Input field '{field_name}' refers to missing component '{binding.component_id}'")
        elif binding.component_field not in _template(node):
            errors.append(
                f"Input field '{field_name}' refers to missing component field "
                f"'{binding.component_id}.{binding.component_field}'"
            )
    for field_name, binding in api.output_mapping.items():
        if binding.component_id not in nodes:
            errors.append(f"Output field '{field_name}' refers to missing component '{binding.component_id}'")
    return errors


def _acceptance_errors(tests: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for test in tests:
        name = str(test.get("name") or "<unnamed>")
        request = test.get("request")
        expected = test.get("expected")
        if not isinstance(request, dict) or not isinstance(expected, dict):
            errors.append(f"Acceptance test '{name}' request and expected values must be objects")
            continue
        path = request.get("path")
        parsed = urlparse(path) if isinstance(path, str) else None
        if (
            parsed is None
            or not path.startswith("/")
            or path.startswith("//")
            or ".." in PurePosixPath(parsed.path).parts
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            errors.append(f"Acceptance test '{name}' path must be a safe absolute runtime path")
        method = str(request.get("method") or ("POST" if "body" in request else "GET")).upper()
        if method not in {"GET", "POST"}:
            errors.append(f"Acceptance test '{name}' method must be GET or POST")
        expected_status = expected.get("status")
        if not isinstance(expected_status, int) or not _MIN_HTTP_STATUS <= expected_status <= _MAX_HTTP_STATUS:
            errors.append(f"Acceptance test '{name}' expected.status must be a valid HTTP status")
    if _contains_plaintext_secret(tests):
        errors.append("Acceptance tests must not contain plaintext credentials")
    return errors


async def analyze_release(
    session: AsyncSession,
    *,
    user_id: UUID,
    release_version: str,
    agent_flow_version_id: UUID,
    ingestion_flow_version_id: UUID,
    config: OnPremDeploymentConfig,
    api: AgentApiContract,
    acceptance_tests: list[AcceptanceTestCreate] | None = None,
    previous_manifest: dict[str, Any] | None = None,
) -> ReleaseAnalysis:
    root_versions = (
        await session.exec(
            select(FlowVersion).where(
                FlowVersion.user_id == user_id,
                or_(
                    FlowVersion.id == agent_flow_version_id,
                    FlowVersion.id == ingestion_flow_version_id,
                ),
            )
        )
    ).all()
    by_id = {version.id: version for version in root_versions}
    missing = [
        str(version_id) for version_id in (agent_flow_version_id, ingestion_flow_version_id) if version_id not in by_id
    ]
    if missing:
        return ReleaseAnalysis(None, (), (f"Flow Version not found: {', '.join(missing)}",), ())

    agent_version = by_id[agent_flow_version_id]
    ingestion_version = by_id[ingestion_flow_version_id]
    errors = [
        *_validate_json_schema(api.input_schema, "input_schema"),
        *_validate_json_schema(api.output_schema, "output_schema"),
        *_example_errors(api.input_schema, api.request_example, "request_example"),
        *_example_errors(api.output_schema, api.response_example, "response_example"),
    ]
    supported_profile = {
        "orchestrator": "compose",
        "topology": "single",
        "architecture": "amd64",
        "accelerator": "cpu",
        "database": "embedded-postgresql",
        "storage": "local",
        "infrastructure": "bundled",
        "model_runtime": "external",
    }
    configured_profile = config.model_dump(mode="json")
    errors.extend(
        f"Offline MVP requires {name}={expected}"
        for name, expected in supported_profile.items()
        if configured_profile.get(name) != expected
    )
    if config.include_model_weights:
        errors.append("Offline MVP does not support bundled model weights")
    if not config.features.signing:
        errors.append("Offline MVP requires release signing")
    if config.features.clamav or config.features.grafana:
        errors.append("Offline MVP does not yet package optional ClamAV or Grafana services")
    if config.base_image_digest is None:
        errors.append("Offline MVP requires a digest-pinned Runtime base image")
    try:
        json.dumps(api.response_example)
    except (TypeError, ValueError):
        errors.append("response_example must be JSON serializable")
    if _contains_plaintext_secret(api.request_example) or _contains_plaintext_secret(api.response_example):
        errors.append("API examples must not contain plaintext credentials")
    if isinstance(agent_version.data, dict) and isinstance(ingestion_version.data, dict):
        errors.extend(_validate_ingestion_contract(agent_version.data, ingestion_version.data))
        errors.extend(_validate_api_mappings(agent_version.data, api))
    shared_knowledge_aliases = (
        _knowledge_aliases(agent_version.data).intersection(_knowledge_aliases(ingestion_version.data))
        if isinstance(agent_version.data, dict) and isinstance(ingestion_version.data, dict)
        else set()
    )

    subflows, subflow_errors = await _resolve_subflows(
        session,
        user_id=user_id,
        roots=[agent_version, ingestion_version],
    )
    errors.extend(subflow_errors)

    flow_entries: list[dict[str, Any]] = []
    all_warnings: list[str] = []
    secrets = set(config.additional_secret_names)
    endpoints = {str(endpoint) for endpoint in config.external_endpoints}
    services: set[str] = set()
    allowed_sandbox_endpoints: set[str] = set()
    dependency_lock = {kind: [] for kind in _DEPENDENCY_KINDS}
    sandbox = False
    for role, version in [
        ("agent", agent_version),
        ("ingestion", ingestion_version),
        *(("subflow", version) for version in subflows),
    ]:
        (
            entry,
            flow_errors,
            flow_warnings,
            flow_secrets,
            flow_endpoints,
            flow_services,
            flow_allowed_sandbox_endpoints,
            flow_sandbox,
            flow_declared_dependencies,
        ) = _inspect_flow(version)
        entry["declared_dependencies"] = flow_declared_dependencies
        persistence_components = (
            _conversation_persistence_components(version.data) if isinstance(version.data, dict) else []
        )
        entry["conversation_persistence_components"] = persistence_components
        entry["role"] = role
        flow_entries.append(entry)
        errors.extend(flow_errors)
        if not config.store_conversations and persistence_components:
            errors.append(
                f"Flow version {version.id} contains direct conversation storage components "
                f"({', '.join(persistence_components)}) while conversation storage is disabled"
            )
        all_warnings.extend(flow_warnings)
        secrets.update(flow_secrets)
        endpoints.update(flow_endpoints)
        services.update(flow_services)
        allowed_sandbox_endpoints.update(flow_allowed_sandbox_endpoints)
        errors.extend(_merge_declared_dependencies(dependency_lock, flow_declared_dependencies))
        sandbox = sandbox or flow_sandbox

    if dependency_lock["python_packages"]:
        errors.append("Custom Python dependency wheels are not yet available in the offline package")
    if dependency_lock["os_packages"] or dependency_lock["binaries"]:
        errors.append("Offline MVP does not support OS package or external binary dependencies")
    if sandbox:
        errors.append("Risky Flow export is blocked until the sandbox worker and egress policy are available")

    api_version = next_api_version(previous_manifest, api.input_schema, api.output_schema)
    acceptance_payload = (
        [test.model_dump(mode="json") for test in acceptance_tests]
        if acceptance_tests
        else [
            {
                "name": "health",
                "required": True,
                "request": {"path": "/health"},
                "expected": {"status": 200},
            },
            {
                "name": "agent-smoke",
                "required": True,
                "request": {
                    "path": f"/api/{api_version}/agent/run",
                    "body": copy.deepcopy(api.request_example),
                },
                "expected": {
                    "status": 200,
                    "body": copy.deepcopy(api.response_example),
                },
            },
        ]
    )
    errors.extend(_acceptance_errors(acceptance_payload))
    deployment_services = _deployment_services(config, services)
    unsupported_services = sorted(set(deployment_services).difference({"runtime", "postgresql", "redis"}))
    if unsupported_services:
        errors.append(f"Offline MVP does not yet package services: {', '.join(unsupported_services)}")
    manifest = {
        "schema_version": 1,
        "provider": "unnest-on-prem",
        "release_version": release_version,
        "release_digest": canonical_digest(
            {
                "flows": flow_entries,
                "config": config.model_dump(mode="json"),
                "api": api.model_dump(mode="json"),
                "acceptance_tests": acceptance_payload,
            }
        ),
        "flows": flow_entries,
        "api": {
            "version": api_version,
            "input_schema": copy.deepcopy(api.input_schema),
            "output_schema": copy.deepcopy(api.output_schema),
            "request_example": copy.deepcopy(api.request_example),
            "response_example": copy.deepcopy(api.response_example),
            "input_mapping": {name: binding.model_dump(mode="json") for name, binding in api.input_mapping.items()},
            "output_mapping": {name: binding.model_dump(mode="json") for name, binding in api.output_mapping.items()},
            "openapi": build_openapi(
                release_version=release_version,
                api_version=api_version,
                input_schema=api.input_schema,
                output_schema=api.output_schema,
            ),
        },
        "deployment": config.model_dump(mode="json"),
        "services": deployment_services,
        "ports": _deployment_ports(deployment_services),
        "external_endpoints": sorted(endpoints),
        "secret_names": sorted(name for name in secrets if name),
        "dependency_lock": dependency_lock,
        "acceptance_tests": acceptance_payload,
        "knowledge_base_alias": next(iter(shared_knowledge_aliases), None),
        "sandbox": {
            "required": sandbox,
            "network_policy": "deny-by-default" if sandbox else "not-applicable",
            "allowed_endpoints": sorted(allowed_sandbox_endpoints),
        },
        "build": {
            "architecture": config.architecture,
            "base_image_digest": config.base_image_digest,
            "dependency_lock_status": "worker-resolution-required",
            "declared_dependency_count": sum(len(values) for values in dependency_lock.values()),
            "sbom_required": True,
            "checksums_required": True,
            "signing_enabled": config.features.signing,
            "signer_fingerprint": None,
        },
        "package": {
            "layout_version": 2,
            "required_files": [
                "manifest/release.json",
                "openapi/openapi.json",
                "reports/sbom.cdx.json",
                "reports/trivy.json",
                "tests/acceptance.json",
                "license/license.json",
                "license/license.sig",
                "compose/compose.yml",
                "signatures/checksums.sig",
            ],
            "required_globs": ["flows/*.json", "images/*.tar"],
        },
    }
    return ReleaseAnalysis(
        manifest if not errors else None,
        tuple(version.id for version in subflows),
        tuple(dict.fromkeys(errors)),
        tuple(dict.fromkeys(all_warnings)),
    )
