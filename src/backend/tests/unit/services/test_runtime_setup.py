import os
import stat

import pytest
from langflow.services.runtime_setup import (
    decrypt_runtime_backup,
    encrypt_runtime_backup,
    generate_age_recovery_key,
)


def test_age_recovery_keys_are_one_time_x25519_pairs():
    first_identity, first_recipient = generate_age_recovery_key()
    second_identity, second_recipient = generate_age_recovery_key()

    assert first_identity.startswith("AGE-SECRET-KEY-1")
    assert first_recipient.startswith("age1")
    assert first_identity == first_identity.upper()
    assert first_recipient == first_recipient.lower()
    assert (first_identity, first_recipient) != (second_identity, second_recipient)


def test_runtime_backup_requires_matching_recovery_identity(tmp_path):
    source = tmp_path / "archive.tar"
    encrypted = tmp_path / "archive.unnest-backup"
    restored = tmp_path / "restored.tar"
    source.write_bytes(os.urandom(2 * 1024 * 1024 + 17))
    identity, recipient = generate_age_recovery_key()

    encrypt_runtime_backup(source, encrypted, recipient)
    decrypt_runtime_backup(encrypted, restored, identity)

    assert restored.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(encrypted.stat().st_mode) == 0o600

    wrong_identity, _wrong_recipient = generate_age_recovery_key()
    with pytest.raises(ValueError, match="identity or integrity"):
        decrypt_runtime_backup(encrypted, tmp_path / "wrong.tar", wrong_identity)


def test_runtime_backup_rejects_tampering(tmp_path):
    source = tmp_path / "archive.tar"
    encrypted = tmp_path / "archive.unnest-backup"
    source.write_bytes(b"runtime-state")
    identity, recipient = generate_age_recovery_key()
    encrypt_runtime_backup(source, encrypted, recipient)
    payload = bytearray(encrypted.read_bytes())
    payload[-20] ^= 1
    encrypted.write_bytes(payload)

    with pytest.raises(ValueError, match="identity or integrity"):
        decrypt_runtime_backup(encrypted, tmp_path / "restored.tar", identity)
