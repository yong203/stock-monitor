from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from stock_monitor.app import MAX_REPORT_BODY_BYTES, _LimitReportBodyMiddleware, create_app
from stock_monitor.database import connect, initialize_database
from stock_monitor.reports import (
    InvestmentReportInput,
    InvestmentReportRepository,
    ReportItem,
    ReportSection,
    ReportSourceInput,
)

TOKEN = "r" * 32
RUN_ID = "8d074de2-7a58-4bd8-b4b8-1f94d057fcfa"


class FakeRuntime:
    def __init__(self) -> None:
        self.client = object()

    async def start(self, database_path: Path) -> None:
        pass

    async def stop(self) -> None:
        pass


def prepare_watchlist(path: Path) -> None:
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                market, symbol, name, security_type, is_common_share, country
            ) VALUES ('NASDAQ', 'AAPL', '애플', 'FOREIGN_STOCK', 1, 'US')
            """
        )
        connection.execute(
            "INSERT INTO watchlist_items (market, symbol, position) VALUES ('NASDAQ', 'AAPL', 0)"
        )


def payload(*, run_id: str = RUN_ID, summary: str = "핵심 변화는 제한적입니다.") -> dict:
    return {
        "run_id": run_id,
        "market": "NASDAQ",
        "symbol": "AAPL",
        "analyzed_at": "2026-09-13T09:00:00+09:00",
        "price": "230.10",
        "currency": "USD",
        "title": "가격 흐름은 균형",
        "summary": summary,
        "short_term_stance": "balanced",
        "medium_term_stance": "favorable",
        "confidence": "medium",
        "change_label": "initial",
        "sections": [
            {
                "title": "시장 데이터",
                "items": [
                    {
                        "kind": "fact",
                        "text": "현재가는 230.10달러입니다.",
                        "source_keys": ["toss-price"],
                    }
                ],
            }
        ],
        "sources": [
            {
                "source_key": "toss-price",
                "kind": "market_data",
                "title": "현재가",
                "publisher": "토스증권",
                "url": "https://developers.tossinvest.com/",
                "published_at": None,
                "retrieved_at": "2026-09-13T09:00:00+09:00",
            }
        ],
        "snapshot": {"price": "230.10"},
    }


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def report_input(
    index: int, analyzed_at: str = "2026-09-13T09:00:00+09:00"
) -> InvestmentReportInput:
    return InvestmentReportInput(
        run_id=f"run-{index}",
        market="NASDAQ",
        symbol="AAPL",
        analyzed_at=analyzed_at,
        price="230.10",
        currency="USD",
        title=f"보고서 {index}",
        summary="핵심 변화는 제한적입니다.",
        short_term_stance="balanced",
        medium_term_stance="favorable",
        confidence="medium",
        change_label="initial",
        sections=(
            ReportSection(
                title="시장 데이터",
                items=(
                    ReportItem(
                        kind="fact",
                        text="현재가는 230.10달러입니다.",
                        source_keys=("toss-price",),
                    ),
                ),
            ),
        ),
        sources=(
            ReportSourceInput(
                source_key="toss-price",
                kind="market_data",
                title="현재가",
                publisher="토스증권",
                url="https://developers.tossinvest.com/",
                published_at=None,
                retrieved_at=analyzed_at,
            ),
        ),
        snapshot={"price": "230.10"},
    )


def test_internal_routes_require_configured_writer_token(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)

    with TestClient(create_app(path, report_writer_token_loader=lambda: None)) as client:
        missing_config = client.get("/internal/v1/report-targets")

    with TestClient(create_app(path, report_writer_token_loader=lambda: TOKEN)) as client:
        unauthorized = client.get("/internal/v1/report-targets")
        wrong = client.get("/internal/v1/report-targets", headers={"Authorization": "Bearer wrong"})

    assert missing_config.status_code == 503
    assert missing_config.json()["detail"]["code"] == "report_writer_not_configured"
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401


def test_report_write_rejects_oversized_stream_before_authentication(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)

    with TestClient(create_app(path, report_writer_token_loader=lambda: TOKEN)) as client:
        response = client.post(
            "/internal/v1/reports",
            content=b"x" * (MAX_REPORT_BODY_BYTES + 1),
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "report_body_too_large"


def test_report_body_limit_counts_streamed_chunks_without_content_length() -> None:
    async def exercise_middleware() -> list[dict]:
        chunks = iter(
            (
                {
                    "type": "http.request",
                    "body": b"x" * MAX_REPORT_BODY_BYTES,
                    "more_body": True,
                },
                {"type": "http.request", "body": b"x", "more_body": False},
            )
        )
        sent: list[dict] = []

        async def receive() -> dict:
            return next(chunks)

        async def send(message: dict) -> None:
            sent.append(message)

        async def inner_app(scope: dict, receive, send) -> None:
            while (await receive()).get("more_body"):
                pass

        middleware = _LimitReportBodyMiddleware(inner_app)
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal/v1/reports",
                "headers": [],
            },
            receive,
            send,
        )
        return sent

    messages = asyncio.run(exercise_middleware())

    assert messages[0]["status"] == 413


def test_lists_targets_and_builds_toss_context(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    calls = []

    async def analysis_builder(client: object, target: object) -> dict[str, object]:
        calls.append((client, target))
        return {
            "quote": {"price": "230.10", "currency": "USD"},
            "metrics": {"returns_percent": {"20": "3.2"}},
            "warnings": [],
        }

    runtime = FakeRuntime()
    app = create_app(
        path,
        enable_live=True,
        runtime_builder=lambda _: runtime,  # type: ignore[arg-type,return-value]
        report_writer_token_loader=lambda: TOKEN,
        report_analysis_builder=analysis_builder,
    )
    with TestClient(app) as client:
        targets = client.get("/internal/v1/report-targets", headers=headers())
        context = client.get("/internal/v1/report-context/nasdaq/aapl", headers=headers())

    assert targets.status_code == 200
    assert targets.json()["targets"][0]["symbol"] == "AAPL"
    assert context.status_code == 200
    assert context.json()["target"]["name"] == "애플"
    assert context.json()["quote"]["price"] == "230.10"
    assert context.json()["analytics"]["metrics"]["returns_percent"]["20"] == "3.2"
    assert context.json()["latest_report"] is None
    assert calls[0][0] is runtime.client


def test_report_write_is_idempotent_and_renders_inline(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    app = create_app(path, report_writer_token_loader=lambda: TOKEN)

    with TestClient(app) as client:
        created = client.post("/internal/v1/reports", json=payload(), headers=headers())
        replay = client.post("/internal/v1/reports", json=payload(), headers=headers())
        conflict = client.post(
            "/internal/v1/reports",
            json=payload(summary="다른 판단입니다."),
            headers=headers(),
        )
        detail = client.get("/instruments/nasdaq/aapl")

    assert created.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["report"]["id"] == created.json()["report"]["id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "report_run_id_conflict"
    assert detail.status_code == 200
    assert 'id="report-' in detail.text
    assert "시장 데이터" in detail.text
    assert "사실" in detail.text
    assert "현재가는 230.10달러입니다." in detail.text


def test_report_write_rejects_invalid_or_unwatched_input(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    app = create_app(path, report_writer_token_loader=lambda: TOKEN)
    unsafe = payload(run_id="7c191bdb-325e-45a7-a305-26ac166c5f9c")
    unsafe["sources"][0]["url"] = "javascript:alert(1)"
    missing = payload(run_id="69a96267-ff20-47e9-8142-f6c8e1c48afb")
    missing["symbol"] = "MSFT"

    with TestClient(app) as client:
        invalid_source = client.post("/internal/v1/reports", json=unsafe, headers=headers())
        missing_target = client.post("/internal/v1/reports", json=missing, headers=headers())

    assert invalid_source.status_code == 422
    assert invalid_source.json()["detail"]["code"] == "invalid_source_url"
    assert missing_target.status_code == 404
    assert missing_target.json()["detail"]["code"] == "report_target_not_found"


def test_older_reports_can_be_loaded_until_the_timeline_is_complete(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    repository = InvestmentReportRepository(path)
    for index in range(21):
        repository.save_report(report_input(index))

    with TestClient(create_app(path)) as client:
        first = client.get("/instruments/NASDAQ/AAPL")
        cursor = re.search(r'data-next-cursor="([^"]+)"', first.text)
        assert cursor is not None
        older = client.get(
            "/api/instruments/NASDAQ/AAPL/reports",
            params={"cursor": cursor.group(1)},
        )

    assert first.text.count('class="report-card"') == 20
    assert older.status_code == 200
    assert older.text.count('class="report-card"') == 1
    assert older.headers["x-next-cursor"] == ""
    assert older.headers["cache-control"] == "no-store"


def test_instrument_page_prunes_expired_reports_without_a_new_write(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_watchlist(path)
    repository = InvestmentReportRepository(path)
    repository.save_report(
        report_input(1, "2020-01-01T00:00:00Z"),
        now=datetime(2020, 1, 1, tzinfo=UTC),
    )

    with TestClient(create_app(path)) as client:
        detail = client.get("/instruments/NASDAQ/AAPL")

    assert detail.status_code == 200
    assert "아직 보고서가 없습니다" in detail.text
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM investment_reports").fetchone()[0] == 0
