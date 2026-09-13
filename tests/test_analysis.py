from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from stock_monitor.analysis import TREND_ENDPOINTS, build_toss_analysis, calculate_price_metrics
from stock_monitor.market_data import DailyCandle, MarketDataError, PriceQuote, StockDetails
from stock_monitor.watchlist import WatchlistItem


def candles(count: int) -> tuple[DailyCandle, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        DailyCandle(
            timestamp=start + timedelta(days=index),
            open_price=Decimal(index + 1),
            high_price=Decimal(index + 2),
            low_price=Decimal(index),
            close_price=Decimal(index + 1),
            volume=Decimal(1_000 + index),
            currency="USD",
        )
        for index in range(count)
    )


def stock() -> StockDetails:
    return StockDetails(
        symbol="AAPL",
        name="애플",
        english_name="APPLE INC",
        isin_code="US0378331005",
        market="NASDAQ",
        security_type="STOCK",
        is_common_share=True,
        status="ACTIVE",
        currency="USD",
        shares_outstanding=Decimal("100"),
        leverage_factor=None,
        list_date=None,
        delist_date=None,
        liquidation_trading=None,
        nxt_supported=None,
        krx_trading_suspended=None,
        nxt_trading_suspended=None,
    )


def target(country: str = "US") -> WatchlistItem:
    return WatchlistItem(
        id=1,
        position=0,
        market="NASDAQ" if country == "US" else "KOSPI",
        symbol="AAPL" if country == "US" else "005930",
        name="애플" if country == "US" else "삼성전자",
        security_type="FOREIGN_STOCK" if country == "US" else "STOCK",
        is_common_share=True,
        country=country,
    )


def test_calculates_core_price_metrics_without_pair_length_error() -> None:
    quote = PriceQuote("AAPL", Decimal("201"), "USD", None)

    metrics = calculate_price_metrics(quote, candles(200), stock())

    assert metrics["observations"] == 200
    assert metrics["market_cap"] == "20100"
    assert metrics["moving_averages"]["20"] == "190.5"
    assert metrics["returns_percent"]["5"] == "3.0769"
    assert metrics["annualized_volatility_20d_percent"] is not None
    assert metrics["drawdown_from_200d_high_percent"] == "0.0000"
    assert metrics["position_in_200d_range_percent"] == "100.0000"


def test_breakout_price_keeps_drawdown_and_range_metrics_bounded() -> None:
    quote = PriceQuote("AAPL", Decimal("250"), "USD", None)

    metrics = calculate_price_metrics(quote, candles(200), stock())

    assert metrics["drawdown_from_200d_high_percent"] == "0.0000"
    assert metrics["position_in_200d_range_percent"] == "100.0000"


class FakeClient:
    def __init__(self, *, country: str, fail_optional: bool = False) -> None:
        self.country = country
        self.fail_optional = fail_optional
        self.trends: list[str] = []
        self.fx_calls = 0

    async def get_prices(self, symbols: list[str]) -> tuple[PriceQuote, ...]:
        return (PriceQuote(symbols[0], Decimal("201"), "USD", None),)

    async def get_daily_candles(self, symbol: str, *, count: int) -> tuple[DailyCandle, ...]:
        assert count == 200
        return candles(200)

    async def get_stock_details(self, symbol: str) -> StockDetails:
        return stock()

    async def get_stock_warnings(self, symbol: str) -> tuple[dict[str, object], ...]:
        if self.fail_optional:
            raise MarketDataError("warnings_unavailable")
        return ({"warning_type": "OVERHEATED"},)

    async def get_stock_trend(
        self, symbol: str, trend: str, *, count: int
    ) -> tuple[dict[str, object], ...]:
        self.trends.append(trend)
        if self.fail_optional and trend == "short-selling":
            raise MarketDataError("trend_unavailable")
        return ({"date": "2026-09-12"},)

    async def get_usd_krw_exchange_rate(self) -> dict[str, str]:
        self.fx_calls += 1
        if self.fail_optional:
            raise MarketDataError("exchange_unavailable")
        return {"rate": "1380.5"}


def test_us_analysis_keeps_core_context_when_optional_sources_fail() -> None:
    client = FakeClient(country="US", fail_optional=True)

    result = asyncio.run(build_toss_analysis(client, target("US")))  # type: ignore[arg-type]

    assert result["quote"]["price"] == "201"
    assert result["warnings"] == []
    assert result["kr_trends"] == {}
    assert result["exchange_rate"] is None
    assert result["errors"] == [
        {"source": "warnings", "code": "warnings_unavailable"},
        {"source": "exchange_rate", "code": "exchange_unavailable"},
    ]


def test_kr_analysis_collects_each_market_trend_without_fx() -> None:
    client = FakeClient(country="KR", fail_optional=True)

    result = asyncio.run(build_toss_analysis(client, target("KR")))  # type: ignore[arg-type]

    assert tuple(client.trends) == TREND_ENDPOINTS
    assert client.fx_calls == 0
    assert "short_selling" not in result["kr_trends"]
    assert result["errors"] == [
        {"source": "warnings", "code": "warnings_unavailable"},
        {"source": "short-selling", "code": "trend_unavailable"},
    ]
