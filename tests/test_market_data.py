from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import httpx
import pytest

from stock_monitor.market_data import (
    DailyCandle,
    IssuedToken,
    MarketDataClient,
    MarketDataError,
    TokenManager,
    parse_decimal,
    parse_exchange_rate,
    parse_market_calendar,
    parse_stock_details,
    parse_stock_trend,
    parse_stock_warnings,
    parse_timestamp,
    select_previous_business_day_candle,
)
from stock_monitor.settings import Credentials

CREDENTIALS = Credentials(client_id="test-id", client_secret="test-secret")
TOKEN_BODY = {"access_token": "token-1", "token_type": "Bearer", "expires_in": 3600}


def run(coroutine):
    return asyncio.run(coroutine)


def test_token_manager_double_checks_under_concurrency() -> None:
    calls = 0

    async def issue() -> IssuedToken:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return IssuedToken("shared-token", 3600)

    async def scenario() -> None:
        manager = TokenManager(issue, clock=lambda: 100.0)
        tokens = await asyncio.gather(*(manager.get_token() for _ in range(20)))
        assert calls == 1
        assert {token.generation for token in tokens} == {1}
        assert {token.value for token in tokens} == {"shared-token"}

    run(scenario())


def test_token_manager_uses_monotonic_expiry_and_generation_safe_invalidate() -> None:
    now = 100.0
    calls = 0

    async def issue() -> IssuedToken:
        nonlocal calls
        calls += 1
        return IssuedToken(f"token-{calls}", 100)

    async def scenario() -> None:
        nonlocal now
        manager = TokenManager(issue, clock=lambda: now, refresh_margin_seconds=10)
        first = await manager.get_token()
        now = 189.9
        assert await manager.get_token() is first
        now = 190.0
        second = await manager.get_token()
        assert second.generation == 2
        await manager.invalidate(first.generation)
        assert await manager.get_token() is second
        assert calls == 2

    run(scenario())


def test_prices_wire_contract_and_nullable_timestamp() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth2/token":
            assert request.method == "POST"
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert request.content == (
                b"grant_type=client_credentials&client_id=test-id&client_secret=test-secret"
            )
            return httpx.Response(200, json=TOKEN_BODY)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/prices"
        assert request.url.params["symbols"] == "005930,AAPL"
        assert request.headers["authorization"] == "Bearer token-1"
        assert "x-tossinvest-account" not in request.headers
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "005930",
                        "timestamp": None,
                        "lastPrice": "72000",
                        "currency": "KRW",
                    },
                    {
                        "symbol": "AAPL",
                        "timestamp": "2026-09-11T22:30:00.123+09:00",
                        "lastPrice": "243.26",
                        "currency": "USD",
                    },
                ]
            },
        )

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            quotes = await client.get_prices(["005930", "AAPL"])
        assert quotes[0].timestamp is None
        assert quotes[0].last_price == Decimal("72000")
        assert quotes[1].timestamp is not None
        assert quotes[1].last_price == Decimal("243.26")

    run(scenario())
    assert len(calls) == 2


def test_unauthorized_invalidates_once_and_retries_with_new_token() -> None:
    token_calls = 0
    price_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{token_calls}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        price_tokens.append(request.headers["authorization"])
        if len(price_tokens) == 1:
            return httpx.Response(401, json={"error": {"code": "token-revoked"}})
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "005930",
                        "timestamp": None,
                        "lastPrice": "72000",
                        "currency": "KRW",
                    }
                ]
            },
        )

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            quotes = await client.get_prices(["005930"])
        assert quotes[0].last_price == Decimal("72000")

    run(scenario())
    assert token_calls == 2
    assert price_tokens == ["Bearer token-1", "Bearer token-2"]


def test_second_unauthorized_is_not_retried_and_error_is_sanitized() -> None:
    token_calls = 0
    price_calls = 0
    raw_secret = "raw-provider-message-with-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, price_calls
        if request.url.path == "/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{token_calls}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        price_calls += 1
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "expired-token",
                    "requestId": "request-1",
                    "message": raw_secret,
                }
            },
        )

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(MarketDataError) as caught:
                await client.get_prices(["005930"])
        error = caught.value
        assert str(error) == "unauthorized"
        assert error.status_code == 401
        assert error.provider_code == "expired-token"
        assert error.request_id == "request-1"
        assert raw_secret not in repr(error)

    run(scenario())
    assert token_calls == 2
    assert price_calls == 2


def test_daily_candle_wire_contract_and_previous_day_selection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_BODY)
        assert request.url.path == "/api/v1/candles"
        assert dict(request.url.params) == {
            "symbol": "AAPL",
            "interval": "1d",
            "count": "3",
        }
        return httpx.Response(
            200,
            json={
                "result": {
                    "candles": [
                        _candle("2026-09-11T00:00:00-04:00", "243.26", "USD"),
                        _candle("2026-09-10T00:00:00-04:00", "240.00", "USD"),
                        _candle("2026-09-09T00:00:00-04:00", "239.00", "USD"),
                    ],
                    "nextBefore": None,
                }
            },
        )

    calendar = parse_market_calendar(_us_calendar(), "US")

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            candles = await client.get_daily_candles("AAPL", count=3)
        baseline = select_previous_business_day_candle(candles, calendar)
        assert baseline.trading_date == date(2026, 9, 10)
        assert baseline.close_price == Decimal("240.00")

    run(scenario())


def test_parses_report_stock_context_from_official_shapes() -> None:
    details = parse_stock_details(
        {
            "result": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "englishName": "SamsungElec",
                    "isinCode": "KR7005930003",
                    "market": "KOSPI",
                    "securityType": "STOCK",
                    "isCommonShare": True,
                    "status": "ACTIVE",
                    "currency": "KRW",
                    "sharesOutstanding": "5919637922",
                    "leverageFactor": None,
                    "listDate": "1975-06-11",
                    "delistDate": None,
                    "koreanMarketDetail": {
                        "liquidationTrading": False,
                        "nxtSupported": True,
                        "krxTradingSuspended": False,
                        "nxtTradingSuspended": None,
                    },
                }
            ]
        },
        "005930",
    )
    warnings = parse_stock_warnings(
        {
            "result": [
                {
                    "warningType": "OVERHEATED",
                    "exchange": "KRX",
                    "startDate": "2026-03-20",
                    "endDate": None,
                }
            ]
        }
    )
    trends = parse_stock_trend(
        {
            "result": {
                "nextUntil": None,
                "records": [
                    {
                        "date": "2026-07-17",
                        "updatedAt": "2026-07-17T14:35:08+09:00",
                        "individual": None,
                        "foreigner": {
                            "buyVolume": "2105300",
                            "sellVolume": "1985400",
                            "netBuyVolume": "119900",
                        },
                        "institution": None,
                        "otherCorporation": None,
                        "foreignerHolding": None,
                        "cfd": None,
                    }
                ],
            }
        },
        "investor-trading",
    )

    assert details.shares_outstanding == Decimal("5919637922")
    assert details.isin_code == "KR7005930003"
    assert details.list_date == date(1975, 6, 11)
    assert details.nxt_supported is True
    assert warnings[0]["end_date"] is None
    assert trends[0]["foreigner"]["net_buy_volume"] == "119900"


def test_parses_exchange_rate_with_direction_and_validity_window() -> None:
    result = parse_exchange_rate(
        {
            "result": {
                "baseCurrency": "USD",
                "quoteCurrency": "KRW",
                "rate": "1380.5",
                "midRate": "1375",
                "basisPoint": "40",
                "rateChangeType": "UP",
                "validFrom": "2026-03-25T09:30:00+09:00",
                "validUntil": "2026-03-25T09:31:00+09:00",
            }
        }
    )

    assert result["rate"] == "1380.5"
    assert result["rate_change_type"] == "UP"
    assert result["valid_until"] == "2026-03-25T09:31:00+09:00"


def test_report_context_wire_contracts_use_documented_paths_and_params() -> None:
    requested: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_BODY)
        requested.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v1/stocks":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "symbol": "005930",
                            "name": "삼성전자",
                            "englishName": "SamsungElec",
                            "isinCode": "KR7005930003",
                            "market": "KOSPI",
                            "securityType": "STOCK",
                            "isCommonShare": True,
                            "status": "ACTIVE",
                            "currency": "KRW",
                            "sharesOutstanding": "5919637922",
                            "leverageFactor": None,
                            "listDate": "1975-06-11",
                            "delistDate": None,
                            "koreanMarketDetail": None,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/warnings"):
            return httpx.Response(200, json={"result": []})
        if request.url.path.endswith("/short-selling"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "records": [
                            {
                                "date": "2026-07-16",
                                "updatedAt": "2026-07-17T02:35:00+09:00",
                                "shortSellingVolume": "100",
                                "shortSellingAmount": "7000000",
                                "shortSellingVolumeRate": "0.01",
                                "shortSellingAmountRate": "0.02",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "baseCurrency": "USD",
                    "quoteCurrency": "KRW",
                    "rate": "1380.5",
                    "midRate": "1375",
                    "basisPoint": "40",
                    "rateChangeType": "UP",
                    "validFrom": "2026-03-25T09:30:00+09:00",
                    "validUntil": "2026-03-25T09:31:00+09:00",
                }
            },
        )

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            await client.get_stock_details("005930")
            await client.get_stock_warnings("005930")
            await client.get_stock_trend("005930", "short-selling", count=20)
            await client.get_usd_krw_exchange_rate()

    run(scenario())
    assert requested == [
        ("/api/v1/stocks", {"symbols": "005930"}),
        ("/api/v1/stocks/005930/warnings", {}),
        ("/api/v1/stocks/005930/short-selling", {"count": "20"}),
        (
            "/api/v1/exchange-rate",
            {"baseCurrency": "USD", "quoteCurrency": "KRW"},
        ),
    ]


def test_kr_and_us_market_calendar_wire_and_nullable_sessions() -> None:
    paths: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_BODY)
        paths.append((request.url.path, request.url.params.get("date")))
        if request.url.path.endswith("/KR"):
            return httpx.Response(200, json=_kr_calendar())
        return httpx.Response(200, json=_us_calendar())

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            kr = await client.get_market_calendar("KR", on_date=date(2026, 9, 12))
            us = await client.get_market_calendar("US")
        assert kr.previous_business_day.trading_date == date(2026, 9, 11)
        assert kr.today.regular_market is None
        assert us.previous_business_day.trading_date == date(2026, 9, 10)
        assert us.today.regular_market is not None
        assert us.today.regular_market.end_time.isoformat() == "2026-09-12T05:00:00+09:00"

    run(scenario())
    assert paths == [
        ("/api/v1/market-calendar/KR", "2026-09-12"),
        ("/api/v1/market-calendar/US", None),
    ]


@pytest.mark.parametrize(
    "value",
    [1, 1.0, True, "NaN", "Infinity", "+1", "01", ".5", "1.", "1e3", ""],
)
def test_decimal_parser_rejects_non_contract_values(value: object) -> None:
    with pytest.raises(MarketDataError, match="invalid_decimal"):
        parse_decimal(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-09-11",
        "2026-09-11T22:30:00",
        "2026-09-11 22:30:00+09:00",
        "2026-02-30T22:30:00+09:00",
    ],
)
def test_timestamp_parser_requires_valid_iso_datetime_with_offset(value: object) -> None:
    with pytest.raises(MarketDataError, match="invalid_timestamp"):
        parse_timestamp(value)


def test_missing_previous_business_day_candle_is_explicit() -> None:
    candle = DailyCandle(
        timestamp=parse_timestamp("2026-09-09T00:00:00-04:00"),
        open_price=Decimal("1"),
        high_price=Decimal("1"),
        low_price=Decimal("1"),
        close_price=Decimal("1"),
        volume=Decimal("1"),
        currency="USD",
    )
    with pytest.raises(MarketDataError, match="previous_business_day_candle_not_found"):
        select_previous_business_day_candle([candle], parse_market_calendar(_us_calendar(), "US"))


def test_transport_exception_does_not_expose_raw_message() -> None:
    raw_secret = "network-error-containing-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(raw_secret, request=request)

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(MarketDataError) as caught:
                await client.get_prices(["005930"])
        assert str(caught.value) == "token_transport_error"
        assert raw_secret not in repr(caught.value)

    run(scenario())


def test_price_transport_exception_is_sanitized_after_token_issuance() -> None:
    raw_secret = "price-transport-error-containing-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN_BODY)
        raise httpx.ReadError(raw_secret, request=request)

    async def scenario() -> None:
        async with MarketDataClient(CREDENTIALS, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(MarketDataError) as caught:
                await client.get_prices(["005930"])
        assert str(caught.value) == "market_data_transport_error"
        assert raw_secret not in repr(caught.value)

    run(scenario())


def _candle(timestamp: str, close_price: str, currency: str) -> dict[str, str]:
    return {
        "timestamp": timestamp,
        "openPrice": close_price,
        "highPrice": close_price,
        "lowPrice": close_price,
        "closePrice": close_price,
        "volume": "100",
        "currency": currency,
    }


def _session(start: str, end: str) -> dict[str, str]:
    return {"startTime": start, "endTime": end}


def _kr_calendar() -> dict[str, object]:
    return {
        "result": {
            "today": {"date": "2026-09-12", "integrated": None},
            "previousBusinessDay": {
                "date": "2026-09-11",
                "integrated": {
                    "regularMarket": _session(
                        "2026-09-11T09:00:00+09:00", "2026-09-11T15:30:00+09:00"
                    )
                },
            },
            "nextBusinessDay": {
                "date": "2026-09-14",
                "integrated": {
                    "regularMarket": _session(
                        "2026-09-14T09:00:00+09:00", "2026-09-14T15:30:00+09:00"
                    )
                },
            },
        }
    }


def _us_calendar() -> dict[str, object]:
    return {
        "result": {
            "today": {
                "date": "2026-09-11",
                "dayMarket": None,
                "preMarket": _session("2026-09-11T17:00:00+09:00", "2026-09-11T22:30:00+09:00"),
                "regularMarket": _session("2026-09-11T22:30:00+09:00", "2026-09-12T05:00:00+09:00"),
                "afterMarket": _session("2026-09-12T05:00:00+09:00", "2026-09-12T07:00:00+09:00"),
            },
            "previousBusinessDay": {
                "date": "2026-09-10",
                "dayMarket": None,
                "preMarket": None,
                "regularMarket": _session("2026-09-10T22:30:00+09:00", "2026-09-11T05:00:00+09:00"),
                "afterMarket": None,
            },
            "nextBusinessDay": {
                "date": "2026-09-14",
                "dayMarket": None,
                "preMarket": None,
                "regularMarket": _session("2026-09-14T22:30:00+09:00", "2026-09-15T05:00:00+09:00"),
                "afterMarket": None,
            },
        }
    }
