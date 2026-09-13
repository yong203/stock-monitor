from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stock_monitor import backup as backup_module
from stock_monitor.backup import DatabaseBackupError, create_database_backup


def test_creates_consistent_private_backup_from_wal_database(tmp_path: Path) -> None:
    source_path = tmp_path / "data" / "stock.db"
    source_path.parent.mkdir()
    with sqlite3.connect(source_path) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE observations (value TEXT NOT NULL)")
        source.execute("INSERT INTO observations VALUES ('latest')")
        source.commit()

        result = create_database_backup(
            source_path,
            now=datetime(2026, 9, 13, 12, 34, 56, tzinfo=UTC),
        )

    assert result.path.name == "stock-20260913-123456-000000.db"
    assert result.retained == 1
    assert stat.S_IMODE(result.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    with sqlite3.connect(result.path) as backup:
        assert backup.execute("SELECT value FROM observations").fetchone() == ("latest",)
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_existing_backup_directory_is_made_private(tmp_path: Path) -> None:
    source_path = tmp_path / "stock.db"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE values_table (value INTEGER)")
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir(mode=0o755)
    backup_directory.chmod(0o755)

    create_database_backup(source_path, backup_directory)

    assert stat.S_IMODE(backup_directory.stat().st_mode) == 0o700


def test_prunes_only_older_backups_for_same_database(tmp_path: Path) -> None:
    source_path = tmp_path / "stock.db"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE values_table (value INTEGER)")

    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    oldest = backup_directory / "stock-20260910-000000-000000.db"
    newer = backup_directory / "stock-20260911-000000-000000.db"
    unrelated = backup_directory / "other-20260910-000000-000000.db"
    for index, path in enumerate((oldest, newer, unrelated), start=1):
        path.touch()
        timestamp = index * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))

    result = create_database_backup(
        source_path,
        backup_directory,
        retention=2,
        now=datetime(2026, 9, 13, tzinfo=UTC),
    )

    assert result.retained == 2
    assert not oldest.exists()
    assert newer.exists()
    assert result.path.exists()
    assert unrelated.exists()


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    source_path = tmp_path / "missing.db"

    with pytest.raises(DatabaseBackupError, match="database_not_found"):
        create_database_backup(source_path)

    assert not source_path.exists()
    assert not (tmp_path / "backups").exists()


def test_failed_backup_removes_temporary_file(tmp_path: Path) -> None:
    source_path = tmp_path / "broken.db"
    source_path.write_text("not a sqlite database")
    backup_directory = tmp_path / "backups"

    with pytest.raises(DatabaseBackupError, match="database_backup_failed"):
        create_database_backup(source_path, backup_directory)

    assert list(backup_directory.iterdir()) == []


def test_backup_file_creation_failure_is_safe(monkeypatch, tmp_path: Path) -> None:
    source_path = tmp_path / "stock.db"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE values_table (value INTEGER)")

    def fail_path(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(backup_module, "_available_path", fail_path)

    with pytest.raises(DatabaseBackupError, match="backup_file_unavailable"):
        create_database_backup(source_path)


def test_retention_failure_removes_new_backup(monkeypatch, tmp_path: Path) -> None:
    source_path = tmp_path / "stock.db"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE values_table (value INTEGER)")

    def fail_retention(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(backup_module, "_prune_backups", fail_retention)

    with pytest.raises(DatabaseBackupError, match="backup_retention_failed"):
        create_database_backup(source_path)

    assert list((tmp_path / "backups").iterdir()) == []
