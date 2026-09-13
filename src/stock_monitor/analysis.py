from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime
from decimal import Decimal

from .market_data import DailyCandle, MarketDataClient, MarketDataError, PriceQuote, StockDetails
from .watchlist import WatchlistItem

RETURN_WINDOWS = (5, 20, 60, 120)
AVERAGE_WINDOWS = (5, 20, 60, 120)
TREND_ENDPOINTS = (
    "investor-trading",
    "program-trades",
    "short-selling",
    "securities-lending",
    "credit-trades",
)


async def build_toss_analysis(
    client: MarketDataClient,
    target: WatchlistItem,
) -> dict[str, object]:
    quote = _single_quote(await client.get_prices([target.symbol]), target.symbol)
    candles = await client.get_daily_candles(target.symbol, count=200)
    stock = await client.get_stock_details(target.symbol)
    errors: list[dict[str, str]] = []

    try:
        warnings = list(await client.get_stock_warnings(target.symbol))
    except MarketDataError as error:
        warnings = []
        errors.append({"source": "warnings", "code": error.code})

    kr_trends: dict[str, list[dict[str, object]]] = {}
    if target.country == "KR":
        for trend in TREND_ENDPOINTS:
            try:
                kr_trends[trend.replace("-", "_")] = list(
                    await client.get_stock_trend(target.symbol, trend, count=20)
                )
            except MarketDataError as error:
                errors.append({"source": trend, "code": error.code})

    exchange_rate: dict[str, str] | None = None
    if target.country == "US":
        try:
            exchange_rate = await client.get_usd_krw_exchange_rate()
        except MarketDataError as error:
            errors.append({"source": "exchange_rate", "code": error.code})

    return {
        "collected_at": datetime.now(UTC).isoformat(),
        "quote": _quote_json(quote),
        "stock": _stock_json(stock),
        "metrics": calculate_price_metrics(quote, candles, stock),
        "recent_candles": [_candle_json(candle) for candle in candles[:30]],
        "warnings": warnings,
        "kr_trends": kr_trends,
        "exchange_rate": exchange_rate,
        "errors": errors,
    }


def calculate_price_metrics(
    quote: PriceQuote,
    candles: tuple[DailyCandle, ...],
    stock: StockDetails,
) -> dict[str, object]:
    ordered = sorted(candles, key=lambda candle: candle.timestamp)
    closes = [candle.close_price for candle in ordered]
    volumes = [candle.volume for candle in ordered]
    metrics: dict[str, object] = {
        "observations": len(ordered),
        "latest_candle_date": ordered[-1].trading_date.isoformat() if ordered else None,
        "market_cap": str(quote.last_price * stock.shares_outstanding),
        "returns_percent": {},
        "moving_averages": {},
        "price_vs_average_percent": {},
        "average_volume": {},
        "annualized_volatility_20d_percent": None,
        "drawdown_from_200d_high_percent": None,
        "position_in_200d_range_percent": None,
    }

    returns = metrics["returns_percent"]
    averages = metrics["moving_averages"]
    versus = metrics["price_vs_average_percent"]
    average_volume = metrics["average_volume"]
    assert isinstance(returns, dict)
    assert isinstance(averages, dict)
    assert isinstance(versus, dict)
    assert isinstance(average_volume, dict)

    for window in RETURN_WINDOWS:
        returns[str(window)] = (
            _percent(quote.last_price / closes[-(window + 1)] - 1)
            if len(closes) > window and closes[-(window + 1)] != 0
            else None
        )
    for window in AVERAGE_WINDOWS:
        if len(closes) >= window:
            average = sum(closes[-window:]) / Decimal(window)
            averages[str(window)] = str(average)
            versus[str(window)] = _percent(quote.last_price / average - 1) if average != 0 else None
            average_volume[str(window)] = str(sum(volumes[-window:]) / Decimal(window))
        else:
            averages[str(window)] = None
            versus[str(window)] = None
            average_volume[str(window)] = None

    recent_closes = closes[-21:]
    if len(recent_closes) == 21 and all(value > 0 for value in recent_closes):
        log_returns = [
            math.log(float(current / previous))
            for previous, current in zip(recent_closes, recent_closes[1:], strict=False)
        ]
        metrics["annualized_volatility_20d_percent"] = _float_percent(
            statistics.stdev(log_returns) * math.sqrt(252)
        )

    if len(ordered) >= 200:
        recent = ordered[-200:]
        high = max(quote.last_price, *(candle.high_price for candle in recent))
        low = min(quote.last_price, *(candle.low_price for candle in recent))
        if high != 0:
            metrics["drawdown_from_200d_high_percent"] = _percent(quote.last_price / high - 1)
        if high != low:
            metrics["position_in_200d_range_percent"] = _percent(
                (quote.last_price - low) / (high - low)
            )
    return metrics


def _single_quote(quotes: tuple[PriceQuote, ...], symbol: str) -> PriceQuote:
    matches = [quote for quote in quotes if quote.symbol.upper() == symbol.upper()]
    if len(matches) != 1:
        raise MarketDataError("price_not_found")
    return matches[0]


def _quote_json(quote: PriceQuote) -> dict[str, str | None]:
    return {
        "symbol": quote.symbol,
        "price": str(quote.last_price),
        "currency": quote.currency,
        "provider_at": quote.timestamp.isoformat() if quote.timestamp else None,
    }


def _stock_json(stock: StockDetails) -> dict[str, str | bool | None]:
    return {
        "symbol": stock.symbol,
        "name": stock.name,
        "english_name": stock.english_name,
        "isin_code": stock.isin_code,
        "market": stock.market,
        "security_type": stock.security_type,
        "is_common_share": stock.is_common_share,
        "status": stock.status,
        "currency": stock.currency,
        "shares_outstanding": str(stock.shares_outstanding),
        "leverage_factor": (
            str(stock.leverage_factor) if stock.leverage_factor is not None else None
        ),
        "list_date": stock.list_date.isoformat() if stock.list_date else None,
        "delist_date": stock.delist_date.isoformat() if stock.delist_date else None,
        "liquidation_trading": stock.liquidation_trading,
        "nxt_supported": stock.nxt_supported,
        "krx_trading_suspended": stock.krx_trading_suspended,
        "nxt_trading_suspended": stock.nxt_trading_suspended,
    }


def _candle_json(candle: DailyCandle) -> dict[str, str]:
    return {
        "timestamp": candle.timestamp.isoformat(),
        "open": str(candle.open_price),
        "high": str(candle.high_price),
        "low": str(candle.low_price),
        "close": str(candle.close_price),
        "volume": str(candle.volume),
        "currency": candle.currency,
    }


def _percent(value: Decimal) -> str:
    return str((value * Decimal(100)).quantize(Decimal("0.0001")))


def _float_percent(value: float) -> str:
    return f"{value * 100:.4f}"
