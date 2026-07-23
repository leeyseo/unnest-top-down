"""Master-key handling and secret storage for first-run runtime setup."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

if TYPE_CHECKING:
    from langflow.services.database.models.runtime_configuration import RuntimeConfiguration

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_BITS = 5


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _bech32_encode(prefix: str, payload: bytes) -> str:
    expanded = [ord(character) >> 5 for character in prefix]
    expanded += [0]
    expanded += [ord(character) & 31 for character in prefix]
    accumulator = 0
    bits = 0
    values = []
    for byte in payload:
        accumulator = accumulator << 8 | byte
        bits += 8
        while bits >= _BECH32_BITS:
            bits -= _BECH32_BITS
            values.append((accumulator >> bits) & 31)
    if bits:
        values.append((accumulator << (_BECH32_BITS - bits)) & 31)
    polymod = _bech32_polymod([*expanded, *values, 0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(polymod >> (5 * (5 - index))) & 31 for index in range(6)]
    return prefix + "1" + "".join(_BECH32_CHARSET[value] for value in [*values, *checksum])


def generate_age_recovery_key() -> tuple[str, str]:
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    identity = _bech32_encode("AGE-SECRET-KEY-", private_bytes).upper()
    recipient = _bech32_encode("age", public_bytes)
    return identity, recipient


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
