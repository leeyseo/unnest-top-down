"""Master-key handling and secret storage for first-run runtime setup."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from langflow.services.database.models.runtime_configuration import RuntimeConfiguration


def master_key_path() -> Path:
    return Path(os.getenv("UNNEST_MASTER_KEY_FILE", "/opt/unnest/secrets/master.key"))


def load_or_create_master_key() -> bytes:
    path = master_key_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        msg = "Runtime master key path must not be a symbolic link"
        raise OSError(msg)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(Fernet.generate_key())
            key_file.flush()
            os.fsync(key_file.fileno())

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        msg = "Runtime master key must not be accessible by group or other users"
        raise PermissionError(msg)
    key = path.read_bytes().strip()
    Fernet(key)
    return key


def encrypt_runtime_secrets(key: bytes, values: dict[str, str]) -> str:
    plaintext = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return Fernet(key).encrypt(plaintext).decode()


def decrypt_runtime_secrets(configuration: RuntimeConfiguration) -> dict[str, str]:
    key = load_or_create_master_key()
    if hashlib.sha256(key).hexdigest() != configuration.master_key_fingerprint:
        msg = "Runtime master key does not match the configured database"
        raise InvalidToken(msg)
    value: Any = json.loads(Fernet(key).decrypt(configuration.encrypted_secrets.encode()))
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(secret, str) for name, secret in value.items()
    ):
        msg = "Runtime secret payload is invalid"
        raise ValueError(msg)
    return value


def master_key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()
