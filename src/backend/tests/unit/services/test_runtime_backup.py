import sqlite3

import pytest
from langflow.services.runtime_backup import (
    RuntimeBackupError,
    RuntimeBackupFile,
    create_runtime_backup,
    extract_runtime_backup,
    restore_runtime_backup,
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
    extracted = tmp_path / "extracted"
    extract_runtime_backup(result.path, identity, extracted)

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
    assert (extracted / "documents/version-1/report.txt").read_bytes() == b"offline document"
    assert (extracted / "secrets/master.key").read_bytes() == b"encrypted-database-key"


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


def test_runtime_restore_replaces_database_storage_and_master_key(monkeypatch, tmp_path):
    backup_database = tmp_path / "backup.db"
    with sqlite3.connect(backup_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('restored')")
    backup_key = tmp_path / "backup-master.key"
    backup_key.write_bytes(b"restored-master-key")
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(tmp_path / "backups"))
    identity, recipient = generate_age_recovery_key()
    backup = create_runtime_backup(
        database_url=f"sqlite:///{backup_database}",
        recipient=recipient,
        master_key=backup_key,
        files=[
            RuntimeBackupFile(
                archive_path="storage/runtime-documents/report.txt",
                contents=b"restored document",
            )
        ],
        release_version="2.0.0",
    )

    target_database = tmp_path / "target.db"
    with sqlite3.connect(target_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('old')")
    storage = tmp_path / "storage"
    (storage / "runtime-documents").mkdir(parents=True)
    (storage / "runtime-documents/report.txt").write_bytes(b"old document")
    target_key = tmp_path / "secrets/master.key"
    target_key.parent.mkdir()
    target_key.write_bytes(b"old-master-key")

    result = restore_runtime_backup(
        path=backup.path,
        identity=identity,
        database_url=f"sqlite:///{target_database}",
        storage_directory=storage,
        master_key_destination=target_key,
    )

    with sqlite3.connect(target_database) as connection:
        value = connection.execute("SELECT value FROM state").fetchone()
    assert value == ("restored",)
    assert (storage / "runtime-documents/report.txt").read_bytes() == b"restored document"
    assert target_key.read_bytes() == b"restored-master-key"
    assert result.backup_id == backup.id
    assert result.release_version == "2.0.0"


def test_runtime_restore_rolls_back_files_when_database_restore_fails(monkeypatch, tmp_path):
    backup_database = tmp_path / "backup.db"
    with sqlite3.connect(backup_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
    backup_key = tmp_path / "backup-master.key"
    backup_key.write_bytes(b"restored-master-key")
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(tmp_path / "backups"))
    identity, recipient = generate_age_recovery_key()
    backup = create_runtime_backup(
        database_url=f"sqlite:///{backup_database}",
        recipient=recipient,
        master_key=backup_key,
        files=[
            RuntimeBackupFile(
                archive_path="storage/runtime-documents/report.txt",
                contents=b"restored document",
            )
        ],
        release_version="2.0.0",
    )
    storage = tmp_path / "storage"
    (storage / "runtime-documents").mkdir(parents=True)
    document = storage / "runtime-documents/report.txt"
    document.write_bytes(b"old document")
    target_key = tmp_path / "secrets/master.key"
    target_key.parent.mkdir()
    target_key.write_bytes(b"old-master-key")

    with pytest.raises(RuntimeBackupError, match="does not match"):
        restore_runtime_backup(
            path=backup.path,
            identity=identity,
            database_url="postgresql://runtime@database/runtime",
            storage_directory=storage,
            master_key_destination=target_key,
        )

    assert document.read_bytes() == b"old document"
    assert target_key.read_bytes() == b"old-master-key"


def test_runtime_restore_rejects_a_different_release_before_mutation(monkeypatch, tmp_path):
    backup_database = tmp_path / "backup.db"
    with sqlite3.connect(backup_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
    backup_key = tmp_path / "backup-master.key"
    backup_key.write_bytes(b"new-key")
    monkeypatch.setenv("UNNEST_BACKUP_DIR", str(tmp_path / "backups"))
    identity, recipient = generate_age_recovery_key()
    backup = create_runtime_backup(
        database_url=f"sqlite:///{backup_database}",
        recipient=recipient,
        master_key=backup_key,
        files=[RuntimeBackupFile(archive_path="storage/document.txt", contents=b"new")],
        release_version="2.0.0",
    )
    target_database = tmp_path / "target.db"
    with sqlite3.connect(target_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('old')")
    storage = tmp_path / "storage"
    storage.mkdir()
    document = storage / "document.txt"
    document.write_bytes(b"old")
    target_key = tmp_path / "secrets/master.key"
    target_key.parent.mkdir()
    target_key.write_bytes(b"old-key")

    with pytest.raises(RuntimeBackupError, match="does not match"):
        restore_runtime_backup(
            path=backup.path,
            identity=identity,
            database_url=f"sqlite:///{target_database}",
            storage_directory=storage,
            master_key_destination=target_key,
            expected_release_version="1.0.0",
        )

    with sqlite3.connect(target_database) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("old",)
    assert document.read_bytes() == b"old"
    assert target_key.read_bytes() == b"old-key"
