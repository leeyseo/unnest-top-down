# ruff: noqa: EM101, TRY003
"""Small allowlist proxy and TCP relay used by the Compose sandbox profile."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_MAX_HEADER_BYTES = 64 * 1024
_BUFFER_SIZE = 64 * 1024
_MAX_PORT = 65535


class SandboxNetworkError(ValueError):
    """Raised for an invalid proxy or relay contract."""


@dataclass(frozen=True)
class Target:
    host: str
    port: int


def allowed_targets(bundle: Path) -> frozenset[Target]:
    try:
        manifest = json.loads((bundle / "manifest" / "release.json").read_text(encoding="utf-8"))
        endpoints = manifest["sandbox"]["allowed_endpoints"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise SandboxNetworkError("Sandbox endpoint allowlist is unavailable") from exc
    if not isinstance(endpoints, list):
        raise SandboxNetworkError("Sandbox endpoint allowlist is invalid")
    targets: set[Target] = set()
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise SandboxNetworkError("Sandbox endpoint allowlist is invalid")
        targets.add(Target(parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == "https" else 80)))
    return frozenset(targets)


def _connection_target(method: str, request_target: str) -> Target:
    parsed = urlsplit(f"//{request_target}") if method == "CONNECT" else urlsplit(request_target)
    if not parsed.hostname:
        raise SandboxNetworkError("Proxy request target is invalid")
    default_port = 443 if method == "CONNECT" or parsed.scheme == "https" else 80
    return Target(parsed.hostname.casefold(), parsed.port or default_port)


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(_BUFFER_SIZE):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _proxy_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    targets: frozenset[Target],
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > _MAX_HEADER_BYTES:
            raise SandboxNetworkError("Proxy request headers are too large")
        lines = header.split(b"\r\n")
        request_line = lines[0].decode("ascii")
        method, request_target, version = request_line.split(" ", 2)
        method = method.upper()
        target = _connection_target(method, request_target)
        if target not in targets:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        upstream_reader, upstream_writer = await asyncio.open_connection(target.host, target.port)
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            parsed = urlsplit(request_target)
            relative = parsed.path or "/"
            if parsed.query:
                relative = f"{relative}?{parsed.query}"
            forwarded_headers = []
            for line in lines[1:-2]:
                name, separator, _value = line.partition(b":")
                if not separator:
                    raise SandboxNetworkError("Proxy request header is invalid")
                if name.strip().lower() in {b"host", b"proxy-authorization", b"proxy-connection"}:
                    continue
                forwarded_headers.append(line)
            default_port = 443 if parsed.scheme == "https" else 80
            host_name = f"[{target.host}]" if ":" in target.host else target.host
            host = host_name if target.port == default_port else f"{host_name}:{target.port}"
            upstream_writer.write(
                f"{method} {relative} {version}\r\n".encode("ascii")
                + f"Host: {host}\r\n".encode("ascii")
                + b"\r\n".join(forwarded_headers)
                + b"\r\n\r\n"
            )
            await upstream_writer.drain()
        await asyncio.gather(
            _relay(reader, upstream_writer),
            _relay(upstream_reader, writer),
        )
    except (ConnectionError, OSError, UnicodeError, ValueError, asyncio.IncompleteReadError):
        if not writer.is_closing():
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            with contextlib.suppress(ConnectionError):
                await writer.drain()
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
        writer.close()


async def _relay_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target: Target,
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(target.host, target.port)
        await asyncio.gather(
            _relay(reader, upstream_writer),
            _relay(upstream_reader, writer),
        )
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
        writer.close()


async def serve_proxy(host: str, port: int, bundle: Path) -> None:
    targets = allowed_targets(bundle)
    server = await asyncio.start_server(
        lambda reader, writer: _proxy_connection(reader, writer, targets),
        host,
        port,
    )
    async with server:
        await server.serve_forever()


async def serve_relay(host: str, port: int, target: Target) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: _relay_connection(reader, writer, target),
        host,
        port,
    )
    async with server:
        await server.serve_forever()


def _address(value: str) -> tuple[str, int]:
    host, separator, raw_port = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("address must be host:port")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("address port is invalid") from exc
    if not 1 <= port <= _MAX_PORT:
        raise argparse.ArgumentTypeError("address port is invalid")
    return host, port


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    proxy = subparsers.add_parser("proxy")
    proxy.add_argument("--listen", default="0.0.0.0:8080", type=_address)
    proxy.add_argument(
        "--bundle",
        type=Path,
        default=Path(os.getenv("UNNEST_SANDBOX_RELEASE_BUNDLE", "/opt/unnest/release")),
    )
    relay = subparsers.add_parser("relay")
    relay.add_argument("--listen", default="0.0.0.0:8090", type=_address)
    relay.add_argument("--target", required=True, type=_address)
    args = parser.parse_args()
    if args.command == "proxy":
        asyncio.run(serve_proxy(*args.listen, args.bundle))
    else:
        asyncio.run(serve_relay(*args.listen, Target(*args.target)))


if __name__ == "__main__":
    main()
