from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_monitor.app import create_app
from stock_monitor.database import MARKETS, initialize_database
from stock_monitor.watchlist import Instrument, replace_market_instruments


class FakeLiveService:
    def __init__(self, calendar_status: str = "ok") -> None:
        self.calendar_status = calendar_status

    async def health(self) -> dict[str, object]:
        return {
            "status": "connected",
            "desired": 1,
            "subscribed": 1,
            "subscribers": 0,
            "reconnect_attempt": 0,
            "last_message_age_seconds": 0,
            "error": None,
            "calendar_status": self.calendar_status,
            "calendar_errors": {},
        }


class FakeRuntime:
    def __init__(self, calendar_status: str = "ok") -> None:
        self.service = FakeLiveService(calendar_status)
        self.started = 0
        self.stopped = 0
        self.refreshed = 0

    async def start(self, database_path: Path) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def refresh_watchlist(self, database_path: Path) -> None:
        self.refreshed += 1


def prepare_catalog(path: Path) -> None:
    initialize_database(path)
    for market in MARKETS:
        items = []
        if market == "NASDAQ":
            items = [
                Instrument(
                    market="NASDAQ",
                    symbol="AAPL",
                    name="애플",
                    security_type="FOREIGN_STOCK",
                    is_common_share=True,
                    country="US",
                ),
                Instrument(
                    market="NASDAQ",
                    symbol="QQQ",
                    name="인베스코 QQQ",
                    security_type="FOREIGN_ETF",
                    is_common_share=True,
                    country="US",
                ),
            ]
        replace_market_instruments(path, market, items, "2026-09-12T00:00:00+09:00")


def test_dashboard_and_watchlist_render_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_catalog(path)

    with TestClient(create_app(path)) as client:
        dashboard = client.get("/")
        watchlist = client.get("/watchlist")
        css = client.get("/static/app.css")
        javascript = client.get("/static/app.js")

    assert dashboard.status_code == 200
    assert "관심종목이 비어 있습니다" in dashboard.text
    assert 'lang="ko"' in dashboard.text
    assert dashboard.text.count("data-market-status") == 2
    assert 'data-country="KR"' in dashboard.text
    assert 'data-country="US"' in dashboard.text
    assert 'id="market-state-announcement"' in dashboard.text
    assert watchlist.status_code == 200
    assert "종목 검색" in watchlist.text
    assert css.status_code == 200
    assert "--bg: #0b0f14" in css.text
    assert javascript.status_code == 200
    assert 'new EventSource("/api/quotes/stream")' in javascript.text
    assert '"시세 서버 연결됨"' in javascript.text
    assert "marketAllowsLive" in javascript.text
    assert "markMarketsDisconnected" in javascript.text
    assert 'market.error === "ip_forbidden"' in javascript.text
    assert 'label: "허용 IP 확인 필요"' in javascript.text
    assert "if (!quoteCards.length) return" not in javascript.text


def test_search_add_list_duplicate_and_remove(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_catalog(path)

    with TestClient(create_app(path)) as client:
        search = client.get("/api/instruments/search", params={"q": "애플"})
        added = client.post("/api/watchlist", json={"market": "NASDAQ", "symbol": "AAPL"})
        duplicate = client.post("/api/watchlist", json={"market": "NASDAQ", "symbol": "aapl"})
        listed = client.get("/api/watchlist")
        dashboard = client.get("/")
        removed = client.delete(f"/api/watchlist/{added.json()['id']}")

    assert search.status_code == 200
    assert search.json()["items"][0]["symbol"] == "AAPL"
    assert added.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "watchlist_duplicate"
    assert len(listed.json()["items"]) == 1
    assert "애플" in dashboard.text
    assert removed.status_code == 204


def test_search_requires_complete_catalog(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"

    with TestClient(create_app(path)) as client:
        response = client.get("/api/instruments/search", params={"q": "AAPL"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "stock_catalog_not_ready"

    with TestClient(create_app(path)) as client:
        add_response = client.post("/api/watchlist", json={"market": "NASDAQ", "symbol": "AAPL"})
    assert add_response.status_code == 503
    assert add_response.json()["detail"]["code"] == "stock_catalog_not_ready"


def test_reorder_conflict_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_catalog(path)

    with TestClient(create_app(path)) as client:
        client.post("/api/watchlist", json={"market": "NASDAQ", "symbol": "AAPL"})
        response = client.put("/api/watchlist/order", json={"item_ids": [999]})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "watchlist_order_conflict"


def test_missing_instrument_and_watchlist_item_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_catalog(path)

    with TestClient(create_app(path)) as client:
        missing_instrument = client.post(
            "/api/watchlist", json={"market": "NASDAQ", "symbol": "MISSING"}
        )
        missing_item = client.delete("/api/watchlist/999")

    assert missing_instrument.status_code == 404
    assert missing_instrument.json()["detail"]["code"] == "instrument_not_found"
    assert missing_item.status_code == 404
    assert missing_item.json()["detail"]["code"] == "watchlist_item_not_found"


def test_mutation_payloads_are_strict_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_catalog(path)

    with TestClient(create_app(path)) as client:
        coerced_string = client.post("/api/watchlist", json={"market": 123, "symbol": "AAPL"})
        coerced_integer = client.put("/api/watchlist/order", json={"item_ids": [True]})
        oversized = client.put("/api/watchlist/order", json={"item_ids": list(range(1, 22))})

    assert coerced_string.status_code == 422
    assert coerced_integer.status_code == 422
    assert oversized.status_code == 422


def test_live_runtime_lifecycle_health_and_watchlist_refresh(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_catalog(path)
    runtime = FakeRuntime()

    with TestClient(
        create_app(
            path,
            enable_live=True,
            runtime_builder=lambda _: runtime,  # type: ignore[arg-type,return-value]
        )
    ) as client:
        health = client.get("/health/market-data")
        added = client.post("/api/watchlist", json={"market": "NASDAQ", "symbol": "AAPL"})
        removed = client.delete(f"/api/watchlist/{added.json()['id']}")

    assert health.status_code == 200
    assert health.json()["status"] == "connected"
    assert added.status_code == 201
    assert removed.status_code == 204
    assert runtime.started == 1
    assert runtime.refreshed == 2
    assert runtime.stopped == 1


def test_market_data_health_and_stream_require_configuration(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"

    with TestClient(create_app(path, enable_live=True, runtime_builder=lambda _: None)) as client:
        health = client.get("/health/market-data")
        stream = client.get("/api/quotes/stream")

    assert health.status_code == 503
    assert health.json()["error"] == "toss_not_configured"
    assert stream.status_code == 503
    assert stream.json()["detail"]["code"] == "market_data_not_configured"


def test_market_data_health_is_degraded_when_calendar_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_catalog(path)
    runtime = FakeRuntime("stale")

    with TestClient(
        create_app(
            path,
            enable_live=True,
            runtime_builder=lambda _: runtime,  # type: ignore[arg-type,return-value]
        )
    ) as client:
        health = client.get("/health/market-data")

    assert health.status_code == 503
    assert health.json()["calendar_status"] == "stale"
