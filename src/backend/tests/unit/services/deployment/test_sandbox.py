import pytest
from langflow.services.deployment.sandbox import SandboxWorkerClient


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
