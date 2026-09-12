from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from stock_monitor.database import initialize_database
from stock_monitor.watchlist import (
    DuplicateWatchlistItem,
    Instrument,
    InvalidWatchlistOrder,
    WatchlistFull,
    WatchlistRepository,
    replace_market_instruments,
)


def instrument(symbol: str, market: str = "NASDAQ", name: str | None = None) -> Instrument:
    return Instrument(
        market=market,
        symbol=symbol,
        name=name or symbol,
        security_type="FOREIGN_STOCK",
        is_common_share=True,
        country="US" if market in {"NYSE", "NASDAQ", "AMEX", "US_ETC"} else "KR",
    )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "stock.db"
    initialize_database(path)
    replace_market_instruments(
        path,
        "NASDAQ",
        [instrument("AAPL", name="애플"), instrument("MSFT", name="마이크로소프트")],
        "2026-09-12T00:00:00+09:00",
    )
    return path


@pytest.fixture
def repository(database_path: Path) -> WatchlistRepository:
    return WatchlistRepository(database_path)


def test_add_list_remove_and_case_insensitive_duplicate(repository: WatchlistRepository) -> None:
    added = repository.add_item("nasdaq", "aapl")

    assert added.symbol == "AAPL"
    assert repository.list_items() == [added]
    with pytest.raises(DuplicateWatchlistItem):
        repository.add_item("NASDAQ", "AAPL")
    assert repository.remove_item(added.id) is True
    assert repository.remove_item(added.id) is False


def test_search_prefers_exact_symbol_and_escapes_wildcards(
    repository: WatchlistRepository,
) -> None:
    results = repository.search_instruments("aapl")

    assert [item.symbol for item in results] == ["AAPL"]
    assert repository.search_instruments("%") == []


def test_twentieth_succeeds_and_twenty_first_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    items = [instrument(f"S{index}") for index in range(21)]
    replace_market_instruments(path, "NASDAQ", items, "2026-09-12T00:00:00+09:00")
    repository = WatchlistRepository(path)

    for item in items[:20]:
        repository.add_item(item.market, item.symbol)
    with pytest.raises(WatchlistFull):
        repository.add_item(items[20].market, items[20].symbol)

    assert len(repository.list_items()) == 20


def test_reorder_requires_exact_id_set(repository: WatchlistRepository) -> None:
    first = repository.add_item("NASDAQ", "AAPL")
    second = repository.add_item("NASDAQ", "MSFT")

    assert [item.id for item in repository.reorder_items([second.id, first.id])] == [
        second.id,
        first.id,
    ]
    for invalid in ([first.id], [first.id, first.id], [first.id, 999]):
        with pytest.raises(InvalidWatchlistOrder):
            repository.reorder_items(invalid)
    assert [item.id for item in repository.list_items()] == [second.id, first.id]


def test_sync_marks_missing_instrument_inactive_but_keeps_watchlist(
    database_path: Path, repository: WatchlistRepository
) -> None:
    added = repository.add_item("NASDAQ", "AAPL")

    replace_market_instruments(
        database_path,
        "NASDAQ",
        [instrument("MSFT", name="마이크로소프트")],
        "2026-09-13T00:00:00+09:00",
    )

    assert repository.search_instruments("AAPL") == []
    assert repository.list_items()[0].id == added.id


def test_concurrent_add_respects_limit(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    items = [instrument(f"S{index}") for index in range(21)]
    replace_market_instruments(path, "NASDAQ", items, "2026-09-12T00:00:00+09:00")
    repository = WatchlistRepository(path)
    for item in items[:19]:
        repository.add_item(item.market, item.symbol)

    def add(symbol: str) -> str:
        try:
            repository.add_item("NASDAQ", symbol)
            return "added"
        except WatchlistFull:
            return "full"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(add, ["S19", "S20"]))

    assert sorted(outcomes) == ["added", "full"]
    assert len(repository.list_items()) == 20


def test_concurrent_duplicate_adds_only_once(repository: WatchlistRepository) -> None:
    def add() -> str:
        try:
            repository.add_item("NASDAQ", "AAPL")
            return "added"
        except DuplicateWatchlistItem:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: add(), range(2)))

    assert sorted(outcomes) == ["added", "duplicate"]
    assert [item.symbol for item in repository.list_items()] == ["AAPL"]


def test_catalog_is_not_ready_when_market_generations_differ(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    for market in ("KOSPI", "KOSDAQ", "KR_ETC", "NYSE", "NASDAQ", "AMEX", "US_ETC"):
        replace_market_instruments(path, market, [], "generation-1")
    repository = WatchlistRepository(path)
    assert repository.catalog_ready() is True

    replace_market_instruments(path, "NASDAQ", [instrument("AAPL")], "generation-2")

    assert repository.catalog_ready() is False
