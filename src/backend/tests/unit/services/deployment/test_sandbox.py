import json

import httpx
import pytest
from langflow.services.deployment.sandbox import SANDBOX_FRAME_HEADER, SandboxWorkerClient


def test_sandbox_worker_requires_mutual_tls_configuration(monkeypatch):
    for name in (
        "UNNEST_SANDBOX_WORKER_URL",
        "UNNEST_SANDBOX_WORKER_CA",
        "UNNEST_SANDBOX_WORKER_CERT",
        "UNNEST_SANDBOX_WORKER_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="mTLS configuration is incomplete"):
        SandboxWorkerClient.from_env()


def test_sandbox_worker_rejects_non_tls_url(tmp_path):
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    for path in (ca, cert, key):
        path.touch()

    with pytest.raises(ValueError, match="HTTPS"):
        SandboxWorkerClient(base_url="http://sandbox.internal", ca=ca, cert=cert, key=key)


async def test_sandbox_worker_streams_ingestion_file_after_framed_metadata(tmp_path):
    source = tmp_path / "document.bin"
    source.write_bytes(b"government-document")
    payload = {"flow_role": "ingestion", "attachment": {"size_bytes": source.stat().st_size}}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        metadata_size = SANDBOX_FRAME_HEADER.unpack(body[: SANDBOX_FRAME_HEADER.size])[0]
        metadata_start = SANDBOX_FRAME_HEADER.size
        metadata_end = metadata_start + metadata_size
        assert json.loads(body[metadata_start:metadata_end]) == payload
        assert body[metadata_end:] == source.read_bytes()
        return httpx.Response(200, json={"outputs": [], "session_id": "sandbox"})

    client = object.__new__(SandboxWorkerClient)
    client._client = httpx.AsyncClient(
        base_url="https://sandbox.internal",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.run_ingestion(payload, source)
    finally:
        await client.aclose()

    assert result["session_id"] == "sandbox"
