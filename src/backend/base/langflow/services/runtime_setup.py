"""Master-key handling and secret storage for first-run runtime setup."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:
    from langflow.services.database.models.runtime_configuration import RuntimeConfiguration

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_BITS = 5
_BECH32_CHECKSUM_VALUES = 6
_BYTE_BITS = 8
_X25519_KEY_BYTES = 32
_BACKUP_MAGIC = b"UNNEST-X25519-AES256-GCM\x00\x01"
_BACKUP_SALT_BYTES = 16
_BACKUP_NONCE_BYTES = 12
_BACKUP_TAG_BYTES = 16
_BACKUP_BUFFER_BYTES = 1024 * 1024


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


def _bech32_decode(value: str, expected_prefix: str) -> bytes:
    normalized = value.strip().lower()
    separator = normalized.rfind("1")
    if separator <= 0 or normalized[:separator] != expected_prefix.lower():
        msg = f"Invalid {expected_prefix} recovery key"
        raise ValueError(msg)
    try:
        values = [_BECH32_CHARSET.index(character) for character in normalized[separator + 1 :]]
    except ValueError as exc:
        msg = f"Invalid {expected_prefix} recovery key"
        raise ValueError(msg) from exc
    if len(values) < _BECH32_CHECKSUM_VALUES + 1:
        msg = f"Invalid {expected_prefix} recovery key"
        raise ValueError(msg)
    prefix = normalized[:separator]

    def expanded_prefix(candidate: str) -> list[int]:
        return [
            *(ord(character) >> 5 for character in candidate),
            0,
            *(ord(character) & 31 for character in candidate),
        ]

    valid_checksum = _bech32_polymod([*expanded_prefix(prefix), *values]) == 1
    legacy_uppercase_checksum = _bech32_polymod([*expanded_prefix(prefix.upper()), *values]) == 1
    if not valid_checksum and not legacy_uppercase_checksum:
        msg = f"Invalid {expected_prefix} recovery key checksum"
        raise ValueError(msg)

    accumulator = 0
    bits = 0
    payload = bytearray()
    for item in values[:-_BECH32_CHECKSUM_VALUES]:
        accumulator = accumulator << _BECH32_BITS | item
        bits += _BECH32_BITS
        while bits >= _BYTE_BITS:
            bits -= _BYTE_BITS
            payload.append((accumulator >> bits) & 0xFF)
    if bits and accumulator & ((1 << bits) - 1):
        msg = f"Invalid {expected_prefix} recovery key padding"
        raise ValueError(msg)
    return bytes(payload)


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
    identity = _bech32_encode("age-secret-key-", private_bytes).upper()
    recipient = _bech32_encode("age", public_bytes)
    return identity, recipient


def _backup_key(private_or_public_key: bytes, ephemeral_or_recipient_key: bytes, salt: bytes) -> bytes:
    private_key = X25519PrivateKey.from_private_bytes(private_or_public_key)
    public_key = X25519PublicKey.from_public_bytes(ephemeral_or_recipient_key)
    shared_key = private_key.exchange(public_key)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"unnest-runtime-backup-v1",
    ).derive(shared_key)


def encrypt_runtime_backup(source: Path, destination: Path, recipient: str) -> None:
    recipient_key = _bech32_decode(recipient, "age")
    if len(recipient_key) != _X25519_KEY_BYTES:
        msg = "Invalid age recovery recipient length"
        raise ValueError(msg)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    salt = os.urandom(_BACKUP_SALT_BYTES)
    nonce = os.urandom(_BACKUP_NONCE_BYTES)
    header = _BACKUP_MAGIC + ephemeral_public + salt + nonce
    recipient_public = X25519PublicKey.from_public_bytes(recipient_key)
    shared_key = ephemeral.exchange(recipient_public)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"unnest-runtime-backup-v1",
    ).derive(shared_key)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as destination_file:
            destination_file.write(header)
            while chunk := source_file.read(_BACKUP_BUFFER_BYTES):
                destination_file.write(encryptor.update(chunk))
            destination_file.write(encryptor.finalize())
            destination_file.write(encryptor.tag)
            destination_file.flush()
            os.fsync(destination_file.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def decrypt_runtime_backup(source: Path, destination: Path, identity: str) -> None:
    identity_key = _bech32_decode(identity, "age-secret-key-")
    if len(identity_key) != _X25519_KEY_BYTES:
        msg = "Invalid age recovery identity length"
        raise ValueError(msg)
    header_size = len(_BACKUP_MAGIC) + _X25519_KEY_BYTES + _BACKUP_SALT_BYTES + _BACKUP_NONCE_BYTES
    source_size = source.stat().st_size
    if source_size <= header_size + _BACKUP_TAG_BYTES:
        msg = "Runtime backup is truncated"
        raise ValueError(msg)

    with source.open("rb") as source_file:
        header = source_file.read(header_size)
        if not header.startswith(_BACKUP_MAGIC):
            msg = "Runtime backup format is invalid"
            raise ValueError(msg)
        offset = len(_BACKUP_MAGIC)
        ephemeral_public = header[offset : offset + _X25519_KEY_BYTES]
        offset += _X25519_KEY_BYTES
        salt = header[offset : offset + _BACKUP_SALT_BYTES]
        nonce = header[-_BACKUP_NONCE_BYTES:]
        source_file.seek(-_BACKUP_TAG_BYTES, os.SEEK_END)
        tag = source_file.read(_BACKUP_TAG_BYTES)
        source_file.seek(header_size)
        remaining = source_size - header_size - _BACKUP_TAG_BYTES

        key = _backup_key(identity_key, ephemeral_public, salt)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as destination_file:
                while remaining:
                    chunk = source_file.read(min(_BACKUP_BUFFER_BYTES, remaining))
                    if not chunk:
                        msg = "Runtime backup is truncated"
                        raise ValueError(msg)
                    remaining -= len(chunk)
                    destination_file.write(decryptor.update(chunk))
                destination_file.write(decryptor.finalize())
                destination_file.flush()
                os.fsync(destination_file.fileno())
        except InvalidTag as exc:
            destination.unlink(missing_ok=True)
            msg = "Runtime backup recovery identity or integrity check failed"
            raise ValueError(msg) from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise


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
