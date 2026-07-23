"""Static analysis and deterministic manifest generation for on-prem exports."""

from __future__ import annotations

import copy
import hashlib
import json
import warnings as python_warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID

from lfx.utils.flow_requirements import generate_requirements_from_flow
from sqlalchemy import or_
from sqlmodel import col, select

from langflow.agentic.helpers.code_security import scan_code_security
from langflow.services.database.models.flow.model import Flow
from langflow.services.database.models.flow_version.model import FlowVersion

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from langflow.api.v1.schemas.on_prem_deployments import AgentApiContract, OnPremDeploymentConfig

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


def _validate_json_schema(schema: dict[str, Any], name: str) -> list[str]:
    errors: list[str] = []
    if schema.get("type") != "object":
        return [f"{name} must be a JSON Schema object"]
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        errors.append(f"{name}.properties must be an object")
        properties = {}
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(value, str) for value in required):
        errors.append(f"{name}.required must be a list of field names")
    elif missing := sorted(set(required).difference(properties)):
        errors.append(f"{name}.required refers to unknown fields: {', '.join(missing)}")
    return errors


def _example_errors(schema: dict[str, Any], example: Any, name: str) -> list[str]:
    if not isinstance(example, dict):
        return [f"{name} must be an object"]
    required = schema.get("required", [])
    missing = sorted(set(required).difference(example)) if isinstance(required, list) else []
    return [f"{name} is missing required fields: {', '.join(missing)}"] if missing else []


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


def next_api_version(previous_manifest: dict[str, Any] | None, input_schema: dict[str, Any]) -> str:
    if not previous_manifest:
        return "v1"
    current = str(previous_manifest.get("api", {}).get("version", "v1"))
    previous_schema = previous_manifest.get("api", {}).get("input_schema", {})
    if not _is_breaking(previous_schema, input_schema):
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
) -> tuple[dict[str, Any], list[str], list[str], set[str], set[str], set[str], bool]:
    data = version.data
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("edges"), list):
        return (
            {},
            [f"Flow version {version.id} does not contain a valid nodes/edges graph"],
            [],
            set(),
            set(),
            set(),
            False,
        )

    errors: list[str] = []
    warnings: list[str] = []
    secrets: set[str] = set()
    endpoints: set[str] = set()
    services: set[str] = set()
    models: set[str] = set()
    sandbox = False

    for node in data["nodes"]:
        if not isinstance(node, dict):
            continue
        node_type = _node_type(node)
        lowered_type = node_type.lower()
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
        sandbox,
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


async def analyze_release(
    session: AsyncSession,
    *,
    user_id: UUID,
    release_version: str,
    agent_flow_version_id: UUID,
    ingestion_flow_version_id: UUID,
    config: OnPremDeploymentConfig,
    api: AgentApiContract,
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
        str(version_id)
        for version_id in (agent_flow_version_id, ingestion_flow_version_id)
        if version_id not in by_id
    ]
    if missing:
        return ReleaseAnalysis(None, (), (f"Flow Version not found: {', '.join(missing)}",), ())

    agent_version = by_id[agent_flow_version_id]
    ingestion_version = by_id[ingestion_flow_version_id]
    errors = [
        *_validate_json_schema(api.input_schema, "input_schema"),
        *_validate_json_schema(api.output_schema, "output_schema"),
        *_example_errors(api.input_schema, api.request_example, "request_example"),
    ]
    try:
        json.dumps(api.response_example)
    except (TypeError, ValueError):
        errors.append("response_example must be JSON serializable")
    if _contains_plaintext_secret(api.request_example) or _contains_plaintext_secret(api.response_example):
        errors.append("API examples must not contain plaintext credentials")
    if isinstance(agent_version.data, dict) and isinstance(ingestion_version.data, dict):
        errors.extend(_validate_ingestion_contract(agent_version.data, ingestion_version.data))
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
    sandbox = False
    for role, version in [
        ("agent", agent_version),
        ("ingestion", ingestion_version),
        *(("subflow", version) for version in subflows),
    ]:
        entry, flow_errors, flow_warnings, flow_secrets, flow_endpoints, flow_services, flow_sandbox = _inspect_flow(
            version
        )
        entry["role"] = role
        flow_entries.append(entry)
        errors.extend(flow_errors)
        all_warnings.extend(flow_warnings)
        secrets.update(flow_secrets)
        endpoints.update(flow_endpoints)
        services.update(flow_services)
        sandbox = sandbox or flow_sandbox

    api_version = next_api_version(previous_manifest, api.input_schema)
    manifest = {
        "schema_version": 1,
        "provider": "unnest-on-prem",
        "release_version": release_version,
        "release_digest": canonical_digest(
            {
                "flows": flow_entries,
                "config": config.model_dump(mode="json"),
                "api": api.model_dump(mode="json"),
            }
        ),
        "flows": flow_entries,
        "api": {
            "version": api_version,
            "input_schema": copy.deepcopy(api.input_schema),
            "output_schema": copy.deepcopy(api.output_schema),
            "request_example": copy.deepcopy(api.request_example),
            "response_example": copy.deepcopy(api.response_example),
            "openapi": build_openapi(
                release_version=release_version,
                api_version=api_version,
                input_schema=api.input_schema,
                output_schema=api.output_schema,
            ),
        },
        "deployment": config.model_dump(mode="json"),
        "services": sorted(services),
        "external_endpoints": sorted(endpoints),
        "secret_names": sorted(name for name in secrets if name),
        "knowledge_base_alias": next(iter(shared_knowledge_aliases), None),
        "sandbox": {
            "required": sandbox,
            "network_policy": "deny-by-default" if sandbox else "not-applicable",
        },
        "build": {
            "architecture": config.architecture,
            "base_image_digest": config.base_image_digest,
            "dependency_lock_status": "pending",
            "sbom_required": True,
            "checksums_required": True,
            "signing_enabled": config.features.signing,
        },
    }
    return ReleaseAnalysis(
        manifest if not errors else None,
        tuple(version.id for version in subflows),
        tuple(dict.fromkeys(errors)),
        tuple(dict.fromkeys(all_warnings)),
    )
