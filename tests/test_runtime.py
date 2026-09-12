from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

from stock_monitor.baselines import BaselineRepository
from stock_monitor.database import initialize_database
from stock_monitor.live import AccessToken, LiveDependencyError, QuoteKey
from stock_monitor.market_data import (
    DailyCandle,
    MarketCalendar,
    MarketDataError,
    MarketDay,
    PriceQuote,
    parse_timestamp,
)
from stock_monitor.runtime import TossSnapshotProvider, TossTokenProvider
from stock_monitor.watchlist import Instrument, WatchlistRepository, replace_market_instruments


class FakeClient:
    def __init__(self) -> None:
        self.candle_calls = 0

    async def get_prices(self, symbols: list[str]) -> tuple[PriceQuote, ...]:
        assert symbols == ["AAPL"]
        return (
            PriceQuote(
                "AAPL",
                Decimal("243.26"),
                "USD",
                parse_timestamp("2026-09-12T05:00:00+09:00"),
            ),
        )

    async def get_market_calendar(self, country: str) -> MarketCalendar:
        assert country == "US"
        empty = MarketDay(date(2026, 9, 12), None, None, None, None)
        previous = MarketDay(date(2026, 9, 11), None, None, None, None)
        return MarketCalendar("US", empty, previous, empty)  # type: ignore[arg-type]

    async def get_daily_candles(self, symbol: str, *, count: int) -> tuple[DailyCandle, ...]:
        assert (symbol, count) == ("AAPL", 5)
        self.candle_calls += 1
        price = Decimal("240")
        return (
            DailyCandle(
                parse_timestamp("2026-09-11T00:00:00-04:00"),
                price,
                price,
                price,
                price,
                Decimal("100"),
                "USD",
            ),
        )


def prepare_watchlist(path: Path) -> None:
    initialize_database(path)
    replace_market_instruments(
        path,
        "NASDAQ",
        [Instrument("NASDAQ", "AAPL", "애플", "STOCK", True, "US")],
        "generation",
    )
    WatchlistRepository(path).add_item("NASDAQ", "AAPL")


def test_snapshot_fetches_and_reuses_previous_close(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    client = FakeClient()
    provider = TossSnapshotProvider(client, path)  # type: ignore[arg-type]
    key = QuoteKey("us", "AAPL")

    async def scenario() -> None:
        first = await provider.fetch(AccessToken("token", 1), (key,))
        second = await provider.fetch(AccessToken("token", 1), (key,))
        assert first[key].price == Decimal("243.26")
        assert first[key].previous_close == Decimal("240")
        assert first[key].baseline_date == "2026-09-11"
        assert second[key].previous_close == Decimal("240")

    asyncio.run(scenario())

    assert client.candle_calls == 1
    cached = BaselineRepository(path).get("NASDAQ", "AAPL", "2026-09-11")
    assert cached is not None
    assert cached.previous_close == "240"


def test_snapshot_keeps_price_when_baseline_lookup_fails(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    client = FakeClient()

    async def broken_candles(symbol: str, *, count: int):
        raise MarketDataError("temporary_failure")

    client.get_daily_candles = broken_candles  # type: ignore[method-assign]
    provider = TossSnapshotProvider(client, path)  # type: ignore[arg-type]
    key = QuoteKey("us", "AAPL")

    updates = asyncio.run(provider.fetch(AccessToken("token", 1), (key,)))

    assert updates[key].price == Decimal("243.26")
    assert updates[key].previous_close is None


def test_snapshot_retries_one_rate_limited_candle_request(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    client = FakeClient()
    original = client.get_daily_candles
    attempts = 0
    sleeps: list[float] = []

    async def rate_limited_once(symbol: str, *, count: int):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise MarketDataError("rate_limited", status_code=429, retry_after_seconds="2")
        return await original(symbol, count=count)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client.get_daily_candles = rate_limited_once  # type: ignore[method-assign]
    provider = TossSnapshotProvider(  # type: ignore[arg-type]
        client, path, sleep=record_sleep
    )
    key = QuoteKey("us", "AAPL")

    updates = asyncio.run(provider.fetch(AccessToken("token", 1), (key,)))

    assert attempts == 2
    assert updates[key].previous_close == Decimal("240")
    assert sleeps == [2.0, 0.1, 0.1]


def test_token_provider_maps_forbidden_without_exposing_response() -> None:
    class ForbiddenClient:
        async def get_token(self):
            raise MarketDataError("token_request_failed", status_code=403)

    async def scenario() -> None:
        provider = TossTokenProvider(ForbiddenClient())  # type: ignore[arg-type]
        try:
            await provider.get_token()
        except LiveDependencyError as error:
            assert error.code == "ip_forbidden"
            assert error.refresh_token is False
        else:
            raise AssertionError("expected LiveDependencyError")

    asyncio.run(scenario())
