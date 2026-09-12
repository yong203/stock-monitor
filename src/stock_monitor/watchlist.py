from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .database import MARKETS, connect

MAX_WATCHLIST_ITEMS = 20
ETF_TYPES = {"ETF", "FOREIGN_ETF"}


class WatchlistError(Exception):
    pass


class InstrumentNotFound(WatchlistError):
    pass


class DuplicateWatchlistItem(WatchlistError):
    pass


class WatchlistFull(WatchlistError):
    pass


class InvalidWatchlistOrder(WatchlistError):
    pass


@dataclass(frozen=True)
class Instrument:
    market: str
    symbol: str
    name: str
    security_type: str
    is_common_share: bool
    country: str

    @property
    def is_etf(self) -> bool:
        return self.security_type in ETF_TYPES


@dataclass(frozen=True)
class WatchlistItem(Instrument):
    id: int
    position: int


class WatchlistRepository:
    def __init__(self, database_path: Path):
        self._database_path = database_path

    def list_items(self) -> list[WatchlistItem]:
        with connect(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT w.id, w.market, w.symbol, w.position,
                       i.name, i.security_type, i.is_common_share, i.country
                FROM watchlist_items AS w
                JOIN instruments AS i USING (market, symbol)
                ORDER BY w.position, w.id
                """
            ).fetchall()
        return [_watchlist_item(row) for row in rows]

    def search_instruments(self, query: str, limit: int = 20) -> list[Instrument]:
        normalized = query.strip()
        if not normalized:
            return []
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        contains = f"%{escaped}%"
        prefix = f"{escaped}%"
        with connect(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT market, symbol, name, security_type, is_common_share, country
                FROM instruments
                WHERE active = 1
                  AND (symbol LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR name LIKE ? ESCAPE '\\' COLLATE NOCASE)
                ORDER BY
                    CASE
                        WHEN symbol = ? COLLATE NOCASE THEN 0
                        WHEN symbol LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 1
                        WHEN name LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 2
                        ELSE 3
                    END,
                    name,
                    symbol
                LIMIT ?
                """,
                (contains, contains, normalized, prefix, prefix, min(max(limit, 1), 50)),
            ).fetchall()
        return [_instrument(row) for row in rows]

    def catalog_ready(self) -> bool:
        with connect(self._database_path) as connection:
            synced_markets, generations = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT synced_at) FROM instrument_syncs"
            ).fetchone()
        return synced_markets == len(MARKETS) and generations == 1

    def add_item(self, market: str, symbol: str) -> WatchlistItem:
        normalized_market = market.strip().upper()
        normalized_symbol = symbol.strip().upper()
        if normalized_market not in MARKETS or not normalized_symbol:
            raise InstrumentNotFound

        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                instrument = connection.execute(
                    """
                    SELECT market, symbol, name, security_type, is_common_share, country
                    FROM instruments
                    WHERE market = ? AND symbol = ? AND active = 1
                    """,
                    (normalized_market, normalized_symbol),
                ).fetchone()
                if instrument is None:
                    raise InstrumentNotFound
                if connection.execute(
                    "SELECT 1 FROM watchlist_items WHERE market = ? AND symbol = ?",
                    (normalized_market, normalized_symbol),
                ).fetchone():
                    raise DuplicateWatchlistItem
                if (
                    connection.execute("SELECT COUNT(*) FROM watchlist_items").fetchone()[0]
                    >= MAX_WATCHLIST_ITEMS
                ):
                    raise WatchlistFull
                position = connection.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM watchlist_items"
                ).fetchone()[0]
                cursor = connection.execute(
                    "INSERT INTO watchlist_items (market, symbol, position) VALUES (?, ?, ?)",
                    (instrument["market"], instrument["symbol"], position),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return WatchlistItem(
            id=cursor.lastrowid, position=position, **_instrument_values(instrument)
        )

    def remove_item(self, item_id: int) -> bool:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM watchlist_items WHERE id = ?", (item_id,))
            connection.commit()
            return cursor.rowcount == 1

    def reorder_items(self, item_ids: Sequence[int]) -> list[WatchlistItem]:
        requested = list(item_ids)
        if len(requested) != len(set(requested)):
            raise InvalidWatchlistOrder

        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM watchlist_items ORDER BY position, id"
                    )
                ]
                if set(current) != set(requested):
                    raise InvalidWatchlistOrder
                if requested:
                    offset = connection.execute(
                        "SELECT COALESCE(MAX(position), -1) + COUNT(*) + 1 FROM watchlist_items"
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE watchlist_items SET position = position + ?", (offset,)
                    )
                    connection.executemany(
                        "UPDATE watchlist_items SET position = ? WHERE id = ?",
                        [(position, item_id) for position, item_id in enumerate(requested)],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.list_items()


def replace_market_instruments(
    database_path: Path,
    market: str,
    instruments: Sequence[Instrument],
    synced_at: str,
) -> None:
    if market not in MARKETS or any(instrument.market != market for instrument in instruments):
        raise ValueError("invalid market instruments")
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _replace_market(connection, market, instruments, synced_at)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def replace_instrument_catalog(
    database_path: Path,
    catalog: Mapping[str, Sequence[Instrument]],
    synced_at: str,
) -> None:
    if set(catalog) != set(MARKETS):
        raise ValueError("catalog must contain every supported market")
    if any(item.market != market for market, items in catalog.items() for item in items):
        raise ValueError("invalid market instruments")

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for market in MARKETS:
                _replace_market(connection, market, catalog[market], synced_at)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _replace_market(
    connection: sqlite3.Connection,
    market: str,
    instruments: Sequence[Instrument],
    synced_at: str,
) -> None:
    connection.execute("UPDATE instruments SET active = 0 WHERE market = ?", (market,))
    connection.executemany(
        """
        INSERT INTO instruments (
            market, symbol, name, security_type, is_common_share, country, active
        ) VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT (market, symbol) DO UPDATE SET
            name = excluded.name,
            security_type = excluded.security_type,
            is_common_share = excluded.is_common_share,
            country = excluded.country,
            active = 1
        """,
        [
            (
                item.market,
                item.symbol,
                item.name,
                item.security_type,
                int(item.is_common_share),
                item.country,
            )
            for item in instruments
        ],
    )
    connection.execute(
        """
        INSERT INTO instrument_syncs (market, synced_at) VALUES (?, ?)
        ON CONFLICT (market) DO UPDATE SET synced_at = excluded.synced_at
        """,
        (market, synced_at),
    )


def _instrument(row: sqlite3.Row) -> Instrument:
    return Instrument(**_instrument_values(row))


def _watchlist_item(row: sqlite3.Row) -> WatchlistItem:
    return WatchlistItem(id=row["id"], position=row["position"], **_instrument_values(row))


def _instrument_values(row: sqlite3.Row) -> dict[str, str | bool]:
    return {
        "market": row["market"],
        "symbol": row["symbol"],
        "name": row["name"],
        "security_type": row["security_type"],
        "is_common_share": bool(row["is_common_share"]),
        "country": row["country"],
    }
