from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import stock_monitor.database as database
from stock_monitor.database import (
    SCHEMA_VERSION,
    UnsupportedDatabaseVersion,
    connect,
    initialize_database,
)


def test_initializes_wal_database_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "data" / "stock.db"

    initialize_database(path)
    initialize_database(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"instruments", "instrument_syncs", "watchlist_items"} <= tables


def test_rejects_newer_database(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with pytest.raises(UnsupportedDatabaseVersion):
        initialize_database(path)


def test_migrates_v1_to_v2_without_losing_watchlist(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    monkeypatch.setattr(database, "SCHEMA_VERSION", 1)
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                market, symbol, name, security_type, is_common_share, country
            ) VALUES ('NASDAQ', 'AAPL', '애플', 'STOCK', 1, 'US')
            """
        )
        connection.execute(
            "INSERT INTO watchlist_items (market, symbol, position) VALUES ('NASDAQ', 'AAPL', 0)"
        )

    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION)
    initialize_database(path)

    with connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT symbol FROM watchlist_items").fetchone()[0] == "AAPL"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quote_baselines'"
        ).fetchone()


def test_rejects_older_database_when_migration_is_missing(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    monkeypatch.setattr(database, "SCHEMA_VERSION", SCHEMA_VERSION + 1)

    with pytest.raises(UnsupportedDatabaseVersion, match="requires a migration"):
        initialize_database(path)


def test_database_constraints_protect_watchlist_integrity(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                market, symbol, name, security_type, is_common_share, country
            ) VALUES ('NASDAQ', 'AAPL', '애플', 'FOREIGN_STOCK', 1, 'US')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO instruments (
                    market, symbol, name, security_type, is_common_share, country
                ) VALUES ('NASDAQ', 'aapl', '중복', 'FOREIGN_STOCK', 1, 'US')
                """
            )
        connection.execute(
            """
            INSERT INTO instruments (
                market, symbol, name, security_type, is_common_share, country
            ) VALUES ('NASDAQ', 'MSFT', '마이크로소프트', 'FOREIGN_STOCK', 1, 'US')
            """
        )
        connection.execute(
            "INSERT INTO watchlist_items (market, symbol, position) VALUES ('NASDAQ', 'AAPL', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO watchlist_items (market, symbol, position) "
                "VALUES ('NASDAQ', 'GOOG', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO watchlist_items (market, symbol, position) "
                "VALUES ('NASDAQ', 'MSFT', 0)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO watchlist_items (market, symbol, position) "
                "VALUES ('NASDAQ', 'aapl', 1)"
            )
