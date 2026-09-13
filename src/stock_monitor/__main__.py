from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backup import DEFAULT_RETENTION, DatabaseBackupError, create_database_backup
from .database import initialize_database
from .diagnostics import run_toss_diagnostic
from .instruments import InstrumentSyncError, sync_instruments
from .settings import (
    CredentialsError,
    UnsafeCredentialsError,
    credentials_path,
    load_credentials,
)
from .settings import database_path as configured_database_path

EXIT_CONFIGURATION = 2
EXIT_UNSAFE_CONFIGURATION = 3
EXIT_BACKUP = 4


def main() -> None:
    parser = argparse.ArgumentParser(prog="stock-monitor")
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=credentials_path(),
        help="TOML credentials path (default: ~/.config/stock-monitor/credentials.toml)",
    )
    parser.add_argument(
        "--database-file",
        type=Path,
        default=configured_database_path(),
        help="SQLite path (default: ~/.local/share/stock-monitor/stock.db)",
    )
    parser.add_argument(
        "--backup-directory",
        type=Path,
        help="Backup directory (default: a backups directory next to the database)",
    )
    parser.add_argument(
        "--backup-retention",
        type=_positive_integer,
        default=DEFAULT_RETENTION,
        help=f"Number of backups to retain (default: {DEFAULT_RETENTION})",
    )
    parser.add_argument("command", choices=["backup-db", "diagnose-toss", "sync-instruments"])
    arguments = parser.parse_args()

    if arguments.command == "backup-db":
        try:
            result = create_database_backup(
                arguments.database_file,
                arguments.backup_directory,
                arguments.backup_retention,
            )
        except DatabaseBackupError as error:
            _finish(False, "backup", error.code, EXIT_BACKUP)
        print(json.dumps(result.safe_dict(), ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(0)

    try:
        credentials = load_credentials(arguments.credentials_file)
    except UnsafeCredentialsError as error:
        _finish(False, "configuration", error.code, EXIT_UNSAFE_CONFIGURATION)
    except CredentialsError as error:
        _finish(False, "configuration", error.code, EXIT_CONFIGURATION)

    if arguments.command == "diagnose-toss":
        result = run_toss_diagnostic(credentials)
    else:
        database_file = arguments.database_file.expanduser()
        initialize_database(database_file)
        try:
            result = sync_instruments(credentials, database_file)
        except InstrumentSyncError as error:
            print(json.dumps(error.safe_dict(), ensure_ascii=False, separators=(",", ":")))
            raise SystemExit(error.exit_code) from None
    print(json.dumps(result.safe_dict(), ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(result.exit_code)


def _finish(ok: bool, stage: str, code: str, exit_code: int) -> None:
    print(
        json.dumps(
            {"ok": ok, "stage": stage, "code": code, "exit_code": exit_code},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    raise SystemExit(exit_code)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


if __name__ == "__main__":
    main()
