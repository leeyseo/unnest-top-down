"""mTLS client for whole-flow execution in the isolated sandbox worker."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from typing_extensions import Self

_WORKER_URL_ENV = "UNNEST_SANDBOX_WORKER_URL"
_WORKER_CA_ENV = "UNNEST_SANDBOX_WORKER_CA"
_WORKER_CERT_ENV = "UNNEST_SANDBOX_WORKER_CERT"
_WORKER_KEY_ENV = "UNNEST_SANDBOX_WORKER_KEY"


class SandboxWorkerClient:
    def __init__(self, *, base_url: str, ca: Path, cert: Path, key: Path) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            msg = "Sandbox worker URL must be an HTTPS URL without embedded credentials"
            raise ValueError(msg)
        for label, path in (("CA", ca), ("certificate", cert), ("private key", key)):
            if not path.is_file():
                msg = f"Sandbox worker {label} file does not exist: {path}"
                raise ValueError(msg)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=str(ca),
            cert=(str(cert), str(key)),
            timeout=httpx.Timeout(30, read=None),
        )

    @classmethod
    def from_env(cls) -> SandboxWorkerClient:
        values = {
            "base_url": os.getenv(_WORKER_URL_ENV),
            "ca": os.getenv(_WORKER_CA_ENV),
            "cert": os.getenv(_WORKER_CERT_ENV),
            "key": os.getenv(_WORKER_KEY_ENV),
        }
        if missing := [name for name, value in values.items() if not value]:
            msg = f"Sandbox worker mTLS configuration is incomplete: {', '.join(missing)}"
            raise ValueError(msg)
        return cls(
            base_url=cast("str", values["base_url"]),
            ca=Path(cast("str", values["ca"])),
            cert=Path(cast("str", values["cert"])),
            key=Path(cast("str", values["key"])),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/v1/flows/run", json=payload)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            msg = "Sandbox worker returned an invalid response"
            raise TypeError(msg)
        return value

    async def stream(self, payload: dict[str, Any]) -> httpx.Response:
        request = self._client.build_request("POST", "/v1/flows/stream", json=payload)
        response = await self._client.send(request, stream=True)
        try:
            response.raise_for_status()
        except httpx.HTTPError:
            await response.aclose()
            raise
        return response
