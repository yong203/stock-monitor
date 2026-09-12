from __future__ import annotations

from pathlib import Path

from stock_monitor.baselines import BaselineRepository, QuoteBaseline
from stock_monitor.database import initialize_database
from stock_monitor.watchlist import Instrument, replace_market_instruments


def test_baseline_round_trip_and_update(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    replace_market_instruments(
        path,
        "NASDAQ",
        [Instrument("NASDAQ", "AAPL", "Apple", "STOCK", True, "US")],
        "generation",
    )
    repository = BaselineRepository(path)
    first = QuoteBaseline("NASDAQ", "AAPL", "2026-09-11", "230.10", "USD", "first")
    updated = QuoteBaseline("NASDAQ", "AAPL", "2026-09-11", "231.20", "USD", "second")

    repository.put(first)
    assert repository.get("NASDAQ", "AAPL", "2026-09-11") == first

    repository.put(updated)
    assert repository.get("NASDAQ", "AAPL", "2026-09-11") == updated
