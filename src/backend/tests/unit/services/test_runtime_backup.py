import sqlite3

import pytest
from langflow.services.runtime_backup import (
    RuntimeBackupError,
    RuntimeBackupFile,
    create_runtime_backup,
    verify_runtime_backup,
)
from langflow.services.runtime_setup import generate_age_recovery_key


def test_runtime_backup_contains_database_documents_key_and_license(monkeypatch, tmp_path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('ready')")
    master_key = tmp_path / "master.key"
    master_key.write_bytes(b"encrypted-database-key")
    license_file = tmp_path / "license.json"
    license_file.write_text('{"issued_to":"agency"}')
    backup_directory = tmp_path / "backups"
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(backup_directory))
    identity, recipient = generate_age_recovery_key()

    result = create_runtime_backup(
        database_url=f"sqlite:///{database}",
        recipient=recipient,
        master_key=master_key,
        files=[
            RuntimeBackupFile(
                archive_path="documents/version-1/report.txt",
                contents=b"offline document",
            )
        ],
        release_version="1.2.3",
        license_files=[license_file],
    )
    manifest = verify_runtime_backup(result.path, identity)

    assert result.path.parent == backup_directory
    assert result.path.stat().st_mode & 0o077 == 0
    assert result.size_bytes == result.path.stat().st_size
    assert len(result.checksum) == 64
    assert manifest["release_version"] == "1.2.3"
    assert manifest["database_format"] == "sqlite3"
    assert {entry["path"] for entry in manifest["entries"]} == {
        "database.dump",
        "secrets/master.key",
        "documents/version-1/report.txt",
        "license/license.json",
    }


def test_runtime_backup_rejects_wrong_identity(monkeypatch, tmp_path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
    master_key = tmp_path / "master.key"
    master_key.write_bytes(b"master-key")
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(tmp_path / "backups"))
    _identity, recipient = generate_age_recovery_key()
    wrong_identity, _wrong_recipient = generate_age_recovery_key()
    result = create_runtime_backup(
        database_url=f"sqlite:///{database}",
        recipient=recipient,
        master_key=master_key,
        files=[],
        release_version="1.0.0",
    )

    with pytest.raises(RuntimeBackupError, match="decrypted or verified"):
        verify_runtime_backup(result.path, wrong_identity)


def test_runtime_backup_rejects_unsafe_entry(monkeypatch, tmp_path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
    master_key = tmp_path / "master.key"
    master_key.write_bytes(b"master-key")
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(tmp_path / "backups"))
    _identity, recipient = generate_age_recovery_key()

    with pytest.raises(RuntimeBackupError, match="Unsafe"):
        create_runtime_backup(
            database_url=f"sqlite:///{database}",
            recipient=recipient,
            master_key=master_key,
            files=[RuntimeBackupFile(archive_path="../secret", contents=b"no")],
            release_version="1.0.0",
        )
