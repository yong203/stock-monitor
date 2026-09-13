from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3
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
                version = 1
            if version == 1 and version < SCHEMA_VERSION:
                _create_schema_v2(connection)
                version = 2
            if version == 2 and version < SCHEMA_VERSION:
                _create_schema_v3(connection)
                version = 3
            if version != SCHEMA_VERSION:
                raise UnsupportedDatabaseVersion(f"database version {version} requires a migration")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
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


def _create_schema_v2(connection: sqlite3.Connection) -> None:
    markets = ", ".join(f"'{market}'" for market in MARKETS)
    connection.execute(
        f"""
        CREATE TABLE quote_baselines (
            market TEXT NOT NULL CHECK (market IN ({markets})),
            symbol TEXT NOT NULL COLLATE NOCASE,
            trading_date TEXT NOT NULL CHECK (length(trading_date) = 10),
            previous_close TEXT NOT NULL CHECK (length(trim(previous_close)) > 0),
            currency TEXT NOT NULL CHECK (length(trim(currency)) > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (market, symbol, trading_date),
            FOREIGN KEY (market, symbol) REFERENCES instruments (market, symbol)
        )
        """
    )


def _create_schema_v3(connection: sqlite3.Connection) -> None:
    markets = ", ".join(f"'{market}'" for market in MARKETS)
    connection.execute(
        f"""
        CREATE TABLE investment_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE CHECK (length(trim(run_id)) > 0),
            market TEXT NOT NULL CHECK (market IN ({markets})),
            symbol TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(symbol)) > 0),
            analyzed_at TEXT NOT NULL CHECK (length(trim(analyzed_at)) > 0),
            created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
            price TEXT NOT NULL CHECK (length(trim(price)) > 0),
            currency TEXT NOT NULL CHECK (length(trim(currency)) > 0),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
            short_term_stance TEXT NOT NULL
                CHECK (short_term_stance IN ('favorable', 'balanced', 'cautious', 'insufficient')),
            medium_term_stance TEXT NOT NULL
                CHECK (medium_term_stance IN ('favorable', 'balanced', 'cautious', 'insufficient')),
            confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
            change_label TEXT NOT NULL
                CHECK (change_label IN (
                    'initial', 'view_changed', 'view_reinforced', 'facts_updated', 'unchanged'
                )),
            sections_json TEXT NOT NULL CHECK (json_valid(sections_json)),
            snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
            FOREIGN KEY (market, symbol) REFERENCES instruments (market, symbol)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX investment_reports_instrument_latest
        ON investment_reports (market, symbol, analyzed_at DESC, id DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE report_sources (
            report_id INTEGER NOT NULL,
            source_key TEXT NOT NULL CHECK (length(trim(source_key)) > 0),
            position INTEGER NOT NULL CHECK (position >= 0),
            kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            publisher TEXT NOT NULL CHECK (length(trim(publisher)) > 0),
            url TEXT NOT NULL CHECK (length(trim(url)) > 0),
            published_at TEXT,
            retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
            PRIMARY KEY (report_id, source_key),
            UNIQUE (report_id, position),
            FOREIGN KEY (report_id) REFERENCES investment_reports (id) ON DELETE CASCADE
        )
        """
    )
