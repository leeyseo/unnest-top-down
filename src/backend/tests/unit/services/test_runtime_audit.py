from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langflow.services.database.models.runtime_audit import RuntimeAuditCheckpoint, RuntimeAuditEvent
from langflow.services.runtime_audit import (
    append_runtime_audit_event,
    create_runtime_audit_checkpoint,
    verify_runtime_audit_chain,
)
from sqlmodel import select


async def test_runtime_audit_chain_detects_event_tampering(async_session):
    first = await append_runtime_audit_event(
        async_session,
        event_type="agent.run",
        resource_type="release",
        resource_id="1.0.0",
        details={"status": "success"},
    )
    second = await append_runtime_audit_event(
        async_session,
        event_type="file.upload",
        resource_type="document",
        resource_id="document-1",
        details={"checksum": "abc"},
    )

    integrity = await verify_runtime_audit_chain(async_session)
    assert integrity["valid"] is True
    assert integrity["events"] == 2
    assert second.previous_hash == first.event_hash

    first.details = {"status": "altered"}
    async_session.add(first)
    await async_session.flush()

    integrity = await verify_runtime_audit_chain(async_session)
    assert integrity["valid"] is False
    assert integrity["issue"] == "event hash mismatch at 1"


async def test_runtime_audit_checkpoint_is_signed_and_verified(async_session):
    await append_runtime_audit_event(
        async_session,
        event_type="upgrade.completed",
        resource_type="release",
        resource_id="2.0.0",
    )
    checkpoint = await create_runtime_audit_checkpoint(
        async_session,
        private_key=Ed25519PrivateKey.generate(),
    )

    integrity = await verify_runtime_audit_chain(async_session)
    assert integrity["valid"] is True
    assert integrity["checkpoints"] == 1

    checkpoint.signature = "aW52YWxpZA=="
    async_session.add(checkpoint)
    await async_session.flush()

    integrity = await verify_runtime_audit_chain(async_session)
    assert integrity["valid"] is False
    assert integrity["issue"] == "checkpoint signature mismatch at 1"

    assert (await async_session.exec(select(RuntimeAuditEvent))).first() is not None
    assert (await async_session.exec(select(RuntimeAuditCheckpoint))).first() is not None
