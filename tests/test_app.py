from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_monitor.app import create_app
from stock_monitor.database import MARKETS, initialize_database
from stock_monitor.watchlist import Instrument, replace_market_instruments


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

    assert dashboard.status_code == 200
    assert "관심종목이 비어 있습니다" in dashboard.text
    assert 'lang="ko"' in dashboard.text
    assert watchlist.status_code == 200
    assert "종목 검색" in watchlist.text
    assert css.status_code == 200
    assert "--bg: #0b0f14" in css.text


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
