from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from .baselines import BaselineRepository, QuoteBaseline
from .live import (
    AccessToken,
    LiveDependencyError,
    LiveQuoteService,
    QuoteKey,
    QuoteUpdate,
    WebSocket,
)
from .market_data import (
    DailyCandle,
    MarketDataClient,
    MarketDataError,
    TokenLease,
    select_previous_business_day_candle,
)
from .settings import CredentialsError, credentials_path, load_credentials
from .watchlist import WatchlistItem, WatchlistRepository

WEBSOCKET_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
BASELINE_REQUEST_INTERVAL_SECONDS = 0.1
MAX_RETRY_AFTER_SECONDS = 10.0


class TossWebSocketConnector:
    async def connect(self, access_token: str) -> WebSocket:
        try:
            return await connect(
                WEBSOCKET_URL,
                additional_headers={"Authorization": f"Bearer {access_token}"},
                proxy=None,
                ping_interval=None,
                open_timeout=10,
                close_timeout=5,
                max_size=64 * 1024,
                max_queue=16,
            )
        except InvalidStatus as error:
            status = error.response.status_code
            if status == 401:
                raise LiveDependencyError("auth_error", refresh_token=True) from None
            if status == 403:
                raise LiveDependencyError("ip_forbidden") from None
            raise LiveDependencyError("websocket_handshake_failed") from None
        except OSError:
            raise LiveDependencyError("connection_lost") from None


class TossTokenProvider:
    def __init__(self, client: MarketDataClient) -> None:
        self._client = client

    async def get_token(self) -> TokenLease:
        try:
            return await self._client.get_token()
        except MarketDataError as error:
            raise _live_error(error) from None

    async def invalidate(self, generation: int) -> None:
        await self._client.invalidate(generation)


class TossSnapshotProvider:
    def __init__(
        self,
        client: MarketDataClient,
        database_path: Path,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._watchlist = WatchlistRepository(database_path)
        self._baselines = BaselineRepository(database_path)
        self._sleep = sleep

    async def fetch(
        self, token: AccessToken, keys: tuple[QuoteKey, ...]
    ) -> dict[QuoteKey, QuoteUpdate]:
        del token
        if not keys:
            return {}
        try:
            prices = await self._client.get_prices(list(dict.fromkeys(key.symbol for key in keys)))
        except MarketDataError as error:
            raise _live_error(error) from None

        baselines = await self._load_baselines(keys)
        price_by_symbol = {price.symbol.upper(): price for price in prices}
        updates: dict[QuoteKey, QuoteUpdate] = {}
        for key in keys:
            price = price_by_symbol.get(key.symbol)
            if price is None:
                continue
            baseline = baselines.get(key)
            updates[key] = QuoteUpdate(
                price=price.last_price,
                currency=price.currency,
                provider_at=price.timestamp.isoformat() if price.timestamp else None,
                previous_close=baseline[1] if baseline else None,
                baseline_date=baseline[0] if baseline else None,
            )
        return updates

    async def _load_baselines(
        self, keys: tuple[QuoteKey, ...]
    ) -> dict[QuoteKey, tuple[str, Decimal]]:
        items = await asyncio.to_thread(self._watchlist.list_items)
        markets = {(item.country.lower(), item.symbol.upper()): item.market for item in items}
        calendars = {}
        for country in sorted({key.market.upper() for key in keys}):
            try:
                calendars[country] = await self._client.get_market_calendar(country)
            except (MarketDataError, ValueError):
                continue

        result = {}
        for key in keys:
            calendar = calendars.get(key.market.upper())
            market = markets.get((key.market, key.symbol))
            if calendar is None or market is None:
                continue
            trading_date = calendar.previous_business_day.trading_date.isoformat()
            cached = await asyncio.to_thread(self._baselines.get, market, key.symbol, trading_date)
            if cached is not None:
                parsed = _baseline_decimal(cached)
                if parsed is not None:
                    result[key] = (trading_date, parsed)
                continue
            try:
                candles = await self._daily_candles_with_retry(key.symbol)
                candle = select_previous_business_day_candle(candles, calendar)
            except MarketDataError:
                continue
            baseline = QuoteBaseline(
                market=market,
                symbol=key.symbol,
                trading_date=trading_date,
                previous_close=str(candle.close_price),
                currency=candle.currency,
                updated_at=datetime.now(UTC).isoformat(),
            )
            await asyncio.to_thread(self._baselines.put, baseline)
            result[key] = (trading_date, candle.close_price)
        return result

    async def _daily_candles_with_retry(self, symbol: str) -> tuple[DailyCandle, ...]:
        for attempt in range(2):
            try:
                candles = await self._client.get_daily_candles(symbol, count=5)
            except MarketDataError as error:
                if error.status_code != 429 or attempt == 1:
                    raise
                retry_after = _retry_after_seconds(error.retry_after_seconds)
                await self._sleep(retry_after)
                continue
            finally:
                await self._sleep(BASELINE_REQUEST_INTERVAL_SECONDS)
            return candles
        raise AssertionError("unreachable")


class MarketDataRuntime:
    def __init__(self, client: MarketDataClient, service: LiveQuoteService) -> None:
        self.client = client
        self.service = service

    async def start(self, database_path: Path) -> None:
        items = await asyncio.to_thread(WatchlistRepository(database_path).list_items)
        await self.service.set_desired(_quote_keys(items))
        await self.service.start()

    async def stop(self) -> None:
        await self.service.stop()
        await self.client.aclose()

    async def refresh_watchlist(self, database_path: Path) -> None:
        items = await asyncio.to_thread(WatchlistRepository(database_path).list_items)
        await self.service.set_desired(_quote_keys(items))


def build_market_data_runtime(database_path: Path) -> MarketDataRuntime | None:
    try:
        credentials = load_credentials(credentials_path())
    except CredentialsError:
        return None
    client = MarketDataClient(credentials)
    service = LiveQuoteService(
        TossTokenProvider(client),
        TossSnapshotProvider(client, database_path),
        TossWebSocketConnector(),
    )
    return MarketDataRuntime(client, service)


def _quote_keys(items: Iterable[WatchlistItem]) -> tuple[QuoteKey, ...]:
    return tuple(QuoteKey(item.country.lower(), item.symbol) for item in items)


def _baseline_decimal(baseline: QuoteBaseline) -> Decimal | None:
    try:
        value = Decimal(baseline.previous_close)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 1.0
    try:
        seconds = float(value)
    except ValueError:
        return 1.0
    return min(max(seconds, 1.0), MAX_RETRY_AFTER_SECONDS)


def _live_error(error: MarketDataError) -> LiveDependencyError:
    if error.status_code == 401:
        return LiveDependencyError("auth_error", refresh_token=True)
    if error.status_code == 403:
        return LiveDependencyError("ip_forbidden")
    if error.status_code == 429:
        return LiveDependencyError("rate_limited")
    return LiveDependencyError(error.code)
