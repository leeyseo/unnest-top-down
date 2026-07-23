"""Offline license verification for the isolated runtime."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa


def _paths() -> tuple[Path, Path, Path]:
    return (
        Path(os.getenv("UNNEST_LICENSE_FILE", "/opt/unnest/license/license.json")),
        Path(os.getenv("UNNEST_LICENSE_SIGNATURE", "/opt/unnest/license/license.sig")),
        Path(os.getenv("UNNEST_LICENSE_PUBLIC_KEY", "/opt/unnest/keys/license.pub")),
    )


def _verify(public_key_blob: bytes, signature_blob: bytes, license_blob: bytes) -> None:
    public_key = serialization.load_pem_public_key(public_key_blob)
    signature = base64.b64decode(signature_blob.strip(), validate=True)
    if isinstance(public_key, ed25519.Ed25519PublicKey | ed448.Ed448PublicKey):
        public_key.verify(signature, license_blob)
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(signature, license_blob, ec.ECDSA(hashes.SHA256()))
    elif isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(signature, license_blob, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise TypeError


def runtime_license_status(release_version: str | None = None) -> dict[str, Any]:
    license_path, signature_path, public_key_path = _paths()
    try:
        license_blob = license_path.read_bytes()
        _verify(public_key_path.read_bytes(), signature_path.read_bytes(), license_blob)
        payload = json.loads(license_blob)
        if not isinstance(payload, dict):
            raise TypeError
        expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        allowed_releases = payload.get("release_versions", [])
        if not isinstance(allowed_releases, list):
            raise TypeError
    except FileNotFoundError:
        return {"valid": False, "reason": "missing", "expires_at": None, "issued_to": None}
    except (InvalidSignature, UnsupportedAlgorithm, KeyError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"valid": False, "reason": "invalid", "expires_at": None, "issued_to": None}

    reason = None
    if expires_at <= datetime.now(timezone.utc):
        reason = "expired"
    elif release_version and allowed_releases and release_version not in allowed_releases:
        reason = "release_not_permitted"
    return {
        "valid": reason is None,
        "reason": reason,
        "expires_at": expires_at.isoformat(),
        "issued_to": payload.get("issued_to") or payload.get("organization"),
    }
