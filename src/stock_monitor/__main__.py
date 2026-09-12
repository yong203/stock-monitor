from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    parser.add_argument("command", choices=["diagnose-toss", "sync-instruments"])
    arguments = parser.parse_args()

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


if __name__ == "__main__":
    main()
