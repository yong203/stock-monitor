from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
MARKETS = ("KOSPI", "KOSDAQ", "KR_ETC", "NYSE", "NASDAQ", "AMEX", "US_ETC")


class UnsupportedDatabaseVersion(Exception):
    pass


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise UnsupportedDatabaseVersion(
                    f"database version {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version == 0:
                _create_schema_v1(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise UnsupportedDatabaseVersion(f"database version {version} requires a migration")
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _create_schema_v1(connection: sqlite3.Connection) -> None:
    markets = ", ".join(f"'{market}'" for market in MARKETS)
    connection.execute(
        f"""
        CREATE TABLE instruments (
            market TEXT NOT NULL CHECK (market IN ({markets})),
            symbol TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(symbol)) > 0),
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            security_type TEXT NOT NULL CHECK (length(trim(security_type)) > 0),
            is_common_share INTEGER NOT NULL CHECK (is_common_share IN (0, 1)),
            country TEXT NOT NULL CHECK (country IN ('KR', 'US')),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            PRIMARY KEY (market, symbol)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE instrument_syncs (
            market TEXT PRIMARY KEY CHECK (market IN ({markets})),
            synced_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE watchlist_items (
            id INTEGER PRIMARY KEY,
            market TEXT NOT NULL CHECK (market IN ({markets})),
            symbol TEXT NOT NULL COLLATE NOCASE,
            position INTEGER NOT NULL CHECK (position >= 0),
            UNIQUE (market, symbol),
            UNIQUE (position),
            FOREIGN KEY (market, symbol) REFERENCES instruments (market, symbol)
        )
        """
    )
