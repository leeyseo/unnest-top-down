import asyncio
import json

from langflow.services.deployment.sandbox_network import (
    Target,
    _proxy_connection,
    allowed_targets,
)


def test_proxy_allowlist_uses_only_signed_manifest_hosts_and_ports(tmp_path):
    (tmp_path / "manifest").mkdir()
    (tmp_path / "manifest/release.json").write_text(
        json.dumps(
            {
                "sandbox": {
                    "allowed_endpoints": [
                        "https://models.internal/v1",
                        "http://10.20.30.40:8080/embeddings",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert allowed_targets(tmp_path) == {
        Target("models.internal", 443),
        Target("10.20.30.40", 8080),
    }


async def test_proxy_rejects_undeclared_network_target():
    server = await asyncio.start_server(
        lambda reader, writer: _proxy_connection(
            reader,
            writer,
            frozenset({Target("models.internal", 443)}),
        ),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"CONNECT metadata.internal:80 HTTP/1.1\r\n"
            b"Host: metadata.internal:80\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

    assert response.startswith(b"HTTP/1.1 403 Forbidden")


async def test_proxy_forwards_declared_http_target():
    forwarded_request = b""

    async def upstream(reader, writer):
        nonlocal forwarded_request
        forwarded_request = await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    proxy_server = await asyncio.start_server(
        lambda reader, writer: _proxy_connection(
            reader,
            writer,
            frozenset({Target("127.0.0.1", upstream_port)}),
        ),
        "127.0.0.1",
        0,
    )
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    async with upstream_server, proxy_server:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            f"GET http://127.0.0.1:{upstream_port}/v1 HTTP/1.1\r\n"
            "Host: undeclared.internal\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=2)
        writer.close()
        await writer.wait_closed()

    assert response.endswith(b"\r\n\r\nok")
    assert f"Host: 127.0.0.1:{upstream_port}\r\n".encode() in forwarded_request
    assert b"undeclared.internal" not in forwarded_request
