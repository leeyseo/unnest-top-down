"""Append and verify the on-premise runtime audit hash chain."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import text
from sqlmodel import col, select

from langflow.services.database.models.runtime_audit import RuntimeAuditCheckpoint, RuntimeAuditEvent

if TYPE_CHECKING:
    from uuid import UUID

    from sqlmodel.ext.asyncio.session import AsyncSession

GENESIS_HASH = "0" * 64
_POSTGRES_LOCK_ID = 0x554E4E455354
_append_lock: asyncio.Lock | None = None
_append_loop: asyncio.AbstractEventLoop | None = None


def _lock_for_current_loop() -> asyncio.Lock:
    global _append_lock, _append_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _append_lock is None or _append_loop is not loop:
        _append_lock = asyncio.Lock()
        _append_loop = loop
    return _append_lock


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _event_hash(
    *,
    sequence: int,
    previous_hash: str,
    event_type: str,
    actor_user_id: UUID | None,
    resource_type: str | None,
    resource_id: str | None,
    details: dict[str, Any],
    occurred_at: datetime,
) -> str:
    payload = {
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "details": details,
        "event_type": event_type,
        "occurred_at": _timestamp(occurred_at),
        "previous_hash": previous_hash,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "sequence": sequence,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def append_runtime_audit_event(
    session: AsyncSession,
    *,
    event_type: str,
    actor_user_id: UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> RuntimeAuditEvent:
    if not event_type.strip():
        msg = "Runtime audit event type is required"
        raise ValueError(msg)
    async with _lock_for_current_loop():
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            await session.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _POSTGRES_LOCK_ID})
        previous = (
            await session.exec(select(RuntimeAuditEvent).order_by(col(RuntimeAuditEvent.sequence).desc()).limit(1))
        ).first()
        sequence = previous.sequence + 1 if previous else 1
        previous_hash = previous.event_hash if previous else GENESIS_HASH
        timestamp = occurred_at or datetime.now(timezone.utc)
        event_details = details or {}
        event = RuntimeAuditEvent(
            sequence=sequence,
            previous_hash=previous_hash,
            event_hash=_event_hash(
                sequence=sequence,
                previous_hash=previous_hash,
                event_type=event_type,
                actor_user_id=actor_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details=event_details,
                occurred_at=timestamp,
            ),
            event_type=event_type,
            actor_user_id=actor_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            details=event_details,
            occurred_at=timestamp,
        )
        session.add(event)
        await session.flush()
        return event


def _checkpoint_message(sequence: int, event_hash: str) -> bytes:
    return f"unnest-runtime-audit-v1\n{sequence}\n{event_hash}\n".encode()


def _private_key_from_environment() -> Ed25519PrivateKey:
    key_path = os.getenv("UNNEST_AUDIT_SIGNING_KEY")
    if not key_path:
        msg = "UNNEST_AUDIT_SIGNING_KEY is not configured"
        raise ValueError(msg)
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        msg = "Runtime audit signing key must be Ed25519"
        raise TypeError(msg)
    return key


async def create_runtime_audit_checkpoint(
    session: AsyncSession,
    *,
    private_key: Ed25519PrivateKey | None = None,
) -> RuntimeAuditCheckpoint:
    latest = (
        await session.exec(select(RuntimeAuditEvent).order_by(col(RuntimeAuditEvent.sequence).desc()).limit(1))
    ).first()
    if latest is None:
        msg = "Runtime audit chain is empty"
        raise ValueError(msg)
    signing_key = private_key or _private_key_from_environment()
    signature = signing_key.sign(_checkpoint_message(latest.sequence, latest.event_hash))
    public_key = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    checkpoint = RuntimeAuditCheckpoint(
        event_sequence=latest.sequence,
        event_hash=latest.event_hash,
        signature=base64.b64encode(signature).decode(),
        public_key=public_key.decode(),
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


async def verify_runtime_audit_chain(session: AsyncSession) -> dict[str, Any]:
    events = (await session.exec(select(RuntimeAuditEvent).order_by(col(RuntimeAuditEvent.sequence)))).all()
    previous_hash = GENESIS_HASH
    expected_sequence = 1
    issue: str | None = None
    hashes: dict[int, str] = {}
    for event in events:
        expected_hash = _event_hash(
            sequence=event.sequence,
            previous_hash=event.previous_hash,
            event_type=event.event_type,
            actor_user_id=event.actor_user_id,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            details=event.details,
            occurred_at=event.occurred_at,
        )
        if event.sequence != expected_sequence:
            issue = f"sequence gap at {expected_sequence}"
            break
        if event.previous_hash != previous_hash:
            issue = f"previous hash mismatch at {event.sequence}"
            break
        if event.event_hash != expected_hash:
            issue = f"event hash mismatch at {event.sequence}"
            break
        hashes[event.sequence] = event.event_hash
        previous_hash = event.event_hash
        expected_sequence += 1

    checkpoint_issue: str | None = None
    checkpoints = (
        await session.exec(select(RuntimeAuditCheckpoint).order_by(col(RuntimeAuditCheckpoint.created_at)))
    ).all()
    for checkpoint in checkpoints:
        if hashes.get(checkpoint.event_sequence) != checkpoint.event_hash:
            checkpoint_issue = f"checkpoint hash mismatch at {checkpoint.event_sequence}"
            break
        try:
            public_key = serialization.load_pem_public_key(checkpoint.public_key.encode())
            if not isinstance(public_key, Ed25519PublicKey):
                raise TypeError
            public_key.verify(
                base64.b64decode(checkpoint.signature, validate=True),
                _checkpoint_message(checkpoint.event_sequence, checkpoint.event_hash),
            )
        except (InvalidSignature, TypeError, ValueError):
            checkpoint_issue = f"checkpoint signature mismatch at {checkpoint.event_sequence}"
            break

    return {
        "valid": issue is None and checkpoint_issue is None,
        "events": len(events),
        "latest_sequence": events[-1].sequence if events else 0,
        "latest_hash": events[-1].event_hash if events else GENESIS_HASH,
        "checkpoints": len(checkpoints),
        "issue": issue or checkpoint_issue,
    }
