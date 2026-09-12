from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from stock_monitor.database import MARKETS, initialize_database
from stock_monitor.instruments import InstrumentSyncError, sync_instruments
from stock_monitor.settings import Credentials
from stock_monitor.watchlist import Instrument, WatchlistRepository, replace_market_instruments

CREDENTIALS = Credentials(client_id="test-id", client_secret="test-secret")
TOKEN = {"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600}


def test_syncs_all_markets_sequentially_and_filters_security_types(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    calls: list[str] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        assert request.method == "GET"
        assert request.url.host == "openapi.tossinvest.com"
        assert request.url.path == "/api/v1/stocks/all"
        assert set(request.url.params.keys()) == {"market", "status"}
        assert request.content == b""
        assert "x-tossinvest-account" not in request.headers
        market = request.url.params["market"]
        calls.append(market)
        assert request.url.params["status"] == "ACTIVE"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": f"S{len(calls)}",
                        "name": f"종목 {len(calls)}",
                        "securityType": "ETF" if market.startswith("K") else "FOREIGN_STOCK",
                        "isCommonShare": True,
                        "isinCode": f"ISIN{len(calls)}",
                    },
                    {
                        "symbol": f"R{len(calls)}",
                        "name": "제외 리츠",
                        "securityType": "REIT",
                        "isCommonShare": True,
                        "isinCode": f"REIT{len(calls)}",
                    },
                ]
            },
            headers={"X-RateLimit-Reset": "0.1"},
        )

    result = sync_instruments(
        CREDENTIALS,
        path,
        transport=httpx.MockTransport(handler),
        sleeper=delays.append,
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )

    assert result.ok is True
    assert result.instruments == len(MARKETS)
    assert calls == list(MARKETS)
    assert delays == [1.0] * (len(MARKETS) - 1)
    assert WatchlistRepository(path).catalog_ready() is True


def test_rejects_invalid_catalog_without_replacing_market(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        return httpx.Response(200, json={"result": [{"symbol": "005930"}]})

    with pytest.raises(InstrumentSyncError, match="invalid_stock_catalog_response") as error:
        sync_instruments(
            CREDENTIALS,
            path,
            transport=httpx.MockTransport(handler),
            sleeper=lambda seconds: None,
        )

    assert error.value.market == "KOSPI"
    assert WatchlistRepository(path).catalog_ready() is False


def test_failed_refresh_preserves_existing_catalog(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    existing = Instrument("NASDAQ", "AAPL", "애플", "FOREIGN_STOCK", True, "US")
    for market in MARKETS:
        replace_market_instruments(
            path,
            market,
            [existing] if market == "NASDAQ" else [],
            "existing-generation",
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        if request.url.params["market"] == "NASDAQ":
            return httpx.Response(503, json={"error": {"code": "maintenance"}})
        return httpx.Response(200, json={"result": []})

    with pytest.raises(InstrumentSyncError, match="maintenance"):
        sync_instruments(
            CREDENTIALS,
            path,
            transport=httpx.MockTransport(handler),
            sleeper=lambda seconds: None,
        )

    repository = WatchlistRepository(path)
    assert repository.catalog_ready() is True
    assert repository.search_instruments("AAPL") == [existing]


def test_retries_rate_limit_once_then_reports_metadata(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    initialize_database(path)
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        return httpx.Response(
            429,
            json={
                "error": {
                    "requestId": "rate-request",
                    "code": "rate-limit-exceeded",
                    "message": "",
                }
            },
            headers={"X-Request-Id": "rate-request", "Retry-After": "2"},
        )

    with pytest.raises(InstrumentSyncError, match="rate_limited") as error:
        sync_instruments(
            CREDENTIALS,
            path,
            transport=httpx.MockTransport(handler),
            sleeper=delays.append,
        )

    assert error.value.exit_code == 12
    assert error.value.provider_code == "rate-limit-exceeded"
    assert error.value.request_id == "rate-request"
    assert error.value.retry_after_seconds == "2"
    assert calls == 3
    assert delays == [2.0]
