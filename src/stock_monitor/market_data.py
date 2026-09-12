from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx

from .settings import Credentials

BASE_URL = "https://openapi.tossinvest.com"
Country = Literal["KR", "US"]

_SYMBOL = re.compile(r"^[A-Za-z0-9.\-]+$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_RETRY_AFTER = re.compile(r"^\d{1,9}$")


class MarketDataError(Exception):
    """A stable, secret-free market data failure."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
        retry_after_seconds: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.provider_code = provider_code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class IssuedToken:
    access_token: str = field(repr=False)
    expires_in: int


@dataclass(frozen=True)
class TokenLease:
    value: str = field(repr=False)
    expires_at: float
    refresh_at: float
    generation: int


@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    last_price: Decimal
    currency: str
    timestamp: datetime | None


@dataclass(frozen=True)
class DailyCandle:
    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: str

    @property
    def trading_date(self) -> date:
        return self.timestamp.date()


@dataclass(frozen=True)
class MarketSession:
    start_time: datetime
    end_time: datetime


@dataclass(frozen=True)
class MarketDay:
    trading_date: date
    day_market: MarketSession | None
    pre_market: MarketSession | None
    regular_market: MarketSession | None
    after_market: MarketSession | None


@dataclass(frozen=True)
class MarketCalendar:
    country: Country
    today: MarketDay
    previous_business_day: MarketDay
    next_business_day: MarketDay


TokenIssuer = Callable[[], Awaitable[IssuedToken]]


class TokenManager:
    """Shares one token and prevents stale 401 responses from clearing a newer token."""

    def __init__(
        self,
        issuer: TokenIssuer,
        *,
        clock: Callable[[], float] = time.monotonic,
        refresh_margin_seconds: float = 30.0,
    ) -> None:
        if refresh_margin_seconds < 0:
            raise ValueError("refresh_margin_seconds_must_be_non_negative")
        self._issuer = issuer
        self._clock = clock
        self._refresh_margin_seconds = refresh_margin_seconds
        self._lock = asyncio.Lock()
        self._current: TokenLease | None = None
        self._generation = 0

    async def get_token(self) -> TokenLease:
        current = self._current
        if self._is_reusable(current):
            return current

        async with self._lock:
            current = self._current
            if self._is_reusable(current):
                return current

            issued = await self._issuer()
            issued_at = self._clock()
            margin = min(self._refresh_margin_seconds, issued.expires_in * 0.1)
            self._generation += 1
            current = TokenLease(
                value=issued.access_token,
                expires_at=issued_at + issued.expires_in,
                refresh_at=issued_at + issued.expires_in - margin,
                generation=self._generation,
            )
            self._current = current
            return current

    async def invalidate(self, generation: int) -> None:
        async with self._lock:
            current = self._current
            if current is None or current.generation != generation:
                return
            self._current = None

    def _is_reusable(self, token: TokenLease | None) -> bool:
        return token is not None and self._clock() < token.refresh_at


class MarketDataClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
        clock: Callable[[], float] = time.monotonic,
        refresh_margin_seconds: float = 30.0,
    ) -> None:
        self._credentials = credentials
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )
        self.token_manager = TokenManager(
            self._issue_token,
            clock=clock,
            refresh_margin_seconds=refresh_margin_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> MarketDataClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def get_token(self) -> TokenLease:
        return await self.token_manager.get_token()

    async def invalidate(self, generation: int) -> None:
        await self.token_manager.invalidate(generation)

    async def get_prices(self, symbols: Sequence[str]) -> tuple[PriceQuote, ...]:
        normalized = _validate_symbols(symbols)
        body = await self._authorized_get(
            "/api/v1/prices",
            params={"symbols": ",".join(normalized)},
        )
        return parse_prices(body)

    async def get_daily_candles(self, symbol: str, *, count: int = 2) -> tuple[DailyCandle, ...]:
        normalized = _validate_symbols([symbol])[0]
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
            raise ValueError("count_must_be_between_1_and_200")
        body = await self._authorized_get(
            "/api/v1/candles",
            params={"symbol": normalized, "interval": "1d", "count": count},
        )
        return parse_daily_candles(body)

    async def get_market_calendar(
        self, country: Country, *, on_date: date | None = None
    ) -> MarketCalendar:
        if country not in ("KR", "US"):
            raise ValueError("country_must_be_kr_or_us")
        if on_date is not None and (not isinstance(on_date, date) or isinstance(on_date, datetime)):
            raise ValueError("on_date_must_be_date")
        params = {} if on_date is None else {"date": on_date.isoformat()}
        body = await self._authorized_get(f"/api/v1/market-calendar/{country}", params=params)
        return parse_market_calendar(body, country)

    async def _issue_token(self) -> IssuedToken:
        try:
            response = await self._client.post(
                "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError:
            raise MarketDataError("token_transport_error") from None

        if response.status_code != 200:
            raise _http_error("token_request_failed", response)
        body = _json_body(response, "invalid_token_response")
        if not isinstance(body, dict):
            raise MarketDataError("invalid_token_response")
        access_token = body.get("access_token")
        token_type = body.get("token_type")
        expires_in = body.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or token_type != "Bearer"
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
        ):
            raise MarketDataError("invalid_token_response")
        return IssuedToken(access_token=access_token, expires_in=expires_in)

    async def _authorized_get(self, path: str, *, params: dict[str, object]) -> Any:
        token = await self.get_token()
        response = await self._get(path, params=params, token=token)
        if response.status_code == 401:
            await self.invalidate(token.generation)
            token = await self.get_token()
            response = await self._get(path, params=params, token=token)
            if response.status_code == 401:
                await self.invalidate(token.generation)

        if response.status_code != 200:
            code = "unauthorized" if response.status_code == 401 else "market_data_request_failed"
            raise _http_error(code, response)
        return _json_body(response, "invalid_market_data_response")

    async def _get(
        self, path: str, *, params: dict[str, object], token: TokenLease
    ) -> httpx.Response:
        try:
            return await self._client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token.value}"},
            )
        except httpx.HTTPError:
            raise MarketDataError("market_data_transport_error") from None


def parse_decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or len(value) > 30 or _DECIMAL.fullmatch(value) is None:
        raise MarketDataError("invalid_decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise MarketDataError("invalid_decimal") from None
    if not parsed.is_finite():
        raise MarketDataError("invalid_decimal")
    return parsed


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise MarketDataError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MarketDataError("invalid_timestamp") from None
    if parsed.utcoffset() is None:
        raise MarketDataError("invalid_timestamp")
    return parsed


def parse_prices(body: Any) -> tuple[PriceQuote, ...]:
    result = _result_list(body)
    quotes: list[PriceQuote] = []
    seen: set[str] = set()
    for raw in result:
        item = _object(raw)
        symbol = _symbol(item.get("symbol"))
        if symbol in seen:
            raise MarketDataError("duplicate_price_symbol")
        seen.add(symbol)
        timestamp_value = item.get("timestamp")
        quotes.append(
            PriceQuote(
                symbol=symbol,
                last_price=parse_decimal(item.get("lastPrice")),
                currency=_currency(item.get("currency")),
                timestamp=None if timestamp_value is None else parse_timestamp(timestamp_value),
            )
        )
    return tuple(quotes)


def parse_daily_candles(body: Any) -> tuple[DailyCandle, ...]:
    root = _object(body)
    result = _object(root.get("result"))
    raw_candles = result.get("candles")
    if not isinstance(raw_candles, list):
        raise MarketDataError("invalid_market_data_response")
    candles: list[DailyCandle] = []
    for raw in raw_candles:
        item = _object(raw)
        candles.append(
            DailyCandle(
                timestamp=parse_timestamp(item.get("timestamp")),
                open_price=parse_decimal(item.get("openPrice")),
                high_price=parse_decimal(item.get("highPrice")),
                low_price=parse_decimal(item.get("lowPrice")),
                close_price=parse_decimal(item.get("closePrice")),
                volume=parse_decimal(item.get("volume")),
                currency=_currency(item.get("currency")),
            )
        )
    return tuple(candles)


def parse_market_calendar(body: Any, country: Country) -> MarketCalendar:
    if country not in ("KR", "US"):
        raise ValueError("country_must_be_kr_or_us")
    root = _object(body)
    result = _object(root.get("result"))
    return MarketCalendar(
        country=country,
        today=_market_day(result.get("today"), country),
        previous_business_day=_market_day(result.get("previousBusinessDay"), country),
        next_business_day=_market_day(result.get("nextBusinessDay"), country),
    )


def select_previous_business_day_candle(
    candles: Sequence[DailyCandle], calendar: MarketCalendar
) -> DailyCandle:
    expected = calendar.previous_business_day.trading_date
    matches = [candle for candle in candles if candle.trading_date == expected]
    if len(matches) != 1:
        raise MarketDataError("previous_business_day_candle_not_found")
    return matches[0]


def _market_day(value: Any, country: Country) -> MarketDay:
    item = _object(value)
    trading_date = _parse_date(item.get("date"))
    if country == "KR":
        integrated_value = item.get("integrated")
        if integrated_value is None:
            sessions: dict[str, Any] = {}
        else:
            sessions = _object(integrated_value)
        day_market = None
    else:
        sessions = item
        day_market = _session(item.get("dayMarket"))
    return MarketDay(
        trading_date=trading_date,
        day_market=day_market,
        pre_market=_session(sessions.get("preMarket")),
        regular_market=_session(sessions.get("regularMarket")),
        after_market=_session(sessions.get("afterMarket")),
    )


def _session(value: Any) -> MarketSession | None:
    if value is None:
        return None
    item = _object(value)
    return MarketSession(
        start_time=parse_timestamp(item.get("startTime")),
        end_time=parse_timestamp(item.get("endTime")),
    )


def _parse_date(value: Any) -> date:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise MarketDataError("invalid_date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise MarketDataError("invalid_date") from None


def _validate_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    if isinstance(symbols, (str, bytes)) or not 1 <= len(symbols) <= 200:
        raise ValueError("symbols_must_contain_between_1_and_200_items")
    normalized = tuple(_symbol(symbol) for symbol in symbols)
    if len(set(normalized)) != len(normalized):
        raise ValueError("symbols_must_be_unique")
    return normalized


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise MarketDataError("invalid_symbol")
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 16:
        raise MarketDataError("invalid_currency")
    return value


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarketDataError("invalid_market_data_response")
    return value


def _result_list(body: Any) -> list[Any]:
    root = _object(body)
    result = root.get("result")
    if not isinstance(result, list):
        raise MarketDataError("invalid_market_data_response")
    return result


def _json_body(response: httpx.Response, error_code: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise MarketDataError(error_code, status_code=response.status_code) from None


def _http_error(code: str, response: httpx.Response) -> MarketDataError:
    provider_code: str | None = None
    request_id: str | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            provider_code = _safe_value(error.get("code"), _SAFE_CODE)
            request_id = _safe_value(error.get("requestId"), _SAFE_REQUEST_ID)
        elif isinstance(error, str):
            provider_code = _safe_value(error, _SAFE_CODE)
    if request_id is None:
        request_id = _safe_value(response.headers.get("X-Request-Id"), _SAFE_REQUEST_ID)
    retry_after = _safe_value(response.headers.get("Retry-After"), _SAFE_RETRY_AFTER)
    return MarketDataError(
        code,
        status_code=response.status_code,
        provider_code=provider_code,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


def _safe_value(value: Any, pattern: re.Pattern[str]) -> str | None:
    if isinstance(value, str) and pattern.fullmatch(value) is not None:
        return value
    return None
