import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langflow.services.runtime_license import runtime_license_status


def _license_files(tmp_path, monkeypatch, *, expires_at="2099-01-01T00:00:00Z"):
    key = Ed25519PrivateKey.generate()
    license_blob = json.dumps(
        {
            "expires_at": expires_at,
            "release_versions": ["1.0.0"],
            "issued_to": "Government Agency",
        },
        sort_keys=True,
    ).encode()
    license_path = tmp_path / "license.json"
    signature_path = tmp_path / "license.sig"
    public_key_path = tmp_path / "license.pub"
    license_path.write_bytes(license_blob)
    signature_path.write_bytes(base64.b64encode(key.sign(license_blob)))
    public_key_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setenv("UNNEST_LICENSE_FILE", str(license_path))
    monkeypatch.setenv("UNNEST_LICENSE_SIGNATURE", str(signature_path))
    monkeypatch.setenv("UNNEST_LICENSE_PUBLIC_KEY", str(public_key_path))
    return license_path


def test_runtime_license_verifies_signature_expiry_and_release_scope(tmp_path, monkeypatch):
    license_path = _license_files(tmp_path, monkeypatch)

    status = runtime_license_status("1.0.0")
    assert status["valid"] is True
    assert status["issued_to"] == "Government Agency"
    assert runtime_license_status("2.0.0")["reason"] == "release_not_permitted"

    license_path.write_bytes(b"tampered")
    assert runtime_license_status("1.0.0")["reason"] == "invalid"


def test_runtime_license_reports_expired_and_missing(tmp_path, monkeypatch):
    _license_files(tmp_path, monkeypatch, expires_at="2000-01-01T00:00:00Z")
    assert runtime_license_status("1.0.0")["reason"] == "expired"

    monkeypatch.setenv("UNNEST_LICENSE_FILE", str(tmp_path / "missing.json"))
    assert runtime_license_status("1.0.0")["reason"] == "missing"
