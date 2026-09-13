from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_RETENTION = 7


class DatabaseBackupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DatabaseBackupResult:
    path: Path
    retained: int

    def safe_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "stage": "complete",
            "code": "database_backup_ok",
            "exit_code": 0,
            "path": str(self.path),
            "retained": self.retained,
        }


def create_database_backup(
    database_path: Path,
    backup_directory: Path | None = None,
    retention: int = DEFAULT_RETENTION,
    *,
    now: datetime | None = None,
) -> DatabaseBackupResult:
    if retention < 1:
        raise ValueError("retention must be at least 1")

    try:
        source_path = database_path.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise DatabaseBackupError("database_not_found") from error
    if not source_path.is_file():
        raise DatabaseBackupError("database_not_regular_file")

    destination_directory = (
        backup_directory.expanduser() if backup_directory else source_path.parent / "backups"
    )
    try:
        destination_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination_directory.chmod(0o700)
    except OSError as error:
        raise DatabaseBackupError("backup_directory_unavailable") from error

    timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S-%f")
    try:
        destination_path = _available_path(
            destination_directory,
            f"{source_path.stem}-{timestamp}",
            source_path.suffix or ".db",
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination_directory,
            prefix=f".{source_path.stem}-backup-",
            suffix=".tmp",
        )
    except OSError as error:
        raise DatabaseBackupError("backup_file_unavailable") from error
    os.close(descriptor)
    temporary_path = Path(temporary_name)

    try:
        with (
            closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(temporary_path)) as destination,
        ):
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise DatabaseBackupError("backup_integrity_check_failed")
        temporary_path.chmod(0o600)
        temporary_path.replace(destination_path)
    except DatabaseBackupError:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise
    except sqlite3.Error as error:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise DatabaseBackupError("database_backup_failed") from error
    except OSError as error:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise DatabaseBackupError("backup_file_unavailable") from error

    try:
        retained = _prune_backups(
            destination_directory,
            source_path.stem,
            source_path.suffix or ".db",
            retention,
        )
    except OSError as error:
        with suppress(OSError):
            destination_path.unlink(missing_ok=True)
        raise DatabaseBackupError("backup_retention_failed") from error
    return DatabaseBackupResult(path=destination_path, retained=retained)


def _available_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    sequence = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{sequence}{suffix}"
        sequence += 1
    return candidate


def _prune_backups(directory: Path, database_stem: str, suffix: str, retention: int) -> int:
    backup_name = re.compile(
        rf"^{re.escape(database_stem)}-\d{{8}}-\d{{6}}-\d{{6}}(?:-\d+)?{re.escape(suffix)}$"
    )
    backups = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and backup_name.fullmatch(path.name)
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for expired in backups[retention:]:
        expired.unlink()
    return min(len(backups), retention)
