from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .database import connect


@dataclass(frozen=True)
class QuoteBaseline:
    market: str
    symbol: str
    trading_date: str
    previous_close: str
    currency: str
    updated_at: str


class BaselineRepository:
    def __init__(self, database_path: Path):
        self._database_path = database_path

    def get(self, market: str, symbol: str, trading_date: str) -> QuoteBaseline | None:
        with connect(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT market, symbol, trading_date, previous_close, currency, updated_at
                FROM quote_baselines
                WHERE market = ? AND symbol = ? AND trading_date = ?
                """,
                (market, symbol, trading_date),
            ).fetchone()
        return QuoteBaseline(**dict(row)) if row else None

    def put(self, baseline: QuoteBaseline) -> None:
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO quote_baselines (
                        market, symbol, trading_date, previous_close, currency, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (market, symbol, trading_date) DO UPDATE SET
                        previous_close = excluded.previous_close,
                        currency = excluded.currency,
                        updated_at = excluded.updated_at
                    """,
                    (
                        baseline.market,
                        baseline.symbol,
                        baseline.trading_date,
                        baseline.previous_close,
                        baseline.currency,
                        baseline.updated_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
