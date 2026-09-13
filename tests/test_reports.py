from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stock_monitor.database import connect, initialize_database
from stock_monitor.reports import (
    InvestmentReportInput,
    InvestmentReportRepository,
    ReportConflict,
    ReportItem,
    ReportSection,
    ReportSourceInput,
    ReportValidationError,
)


def prepare_database(path: Path) -> None:
    initialize_database(path)
    with connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO instruments (
                market, symbol, name, security_type, is_common_share, country
            ) VALUES (?, ?, ?, 'FOREIGN_STOCK', 1, 'US')
            """,
            [
                ("NASDAQ", "AAPL", "애플"),
                ("NASDAQ", "MSFT", "마이크로소프트"),
            ],
        )


def source(key: str = "quote") -> ReportSourceInput:
    return ReportSourceInput(
        source_key=key,
        kind="market_data",
        title="현재가",
        publisher="토스증권",
        url="https://example.test/quotes/AAPL",
        published_at=None,
        retrieved_at="2026-09-13T09:01:00+09:00",
    )


def report(
    run_id: str = "run-1", analyzed_at: str = "2026-09-13T09:00:00+09:00"
) -> InvestmentReportInput:
    return InvestmentReportInput(
        run_id=run_id,
        market="NASDAQ",
        symbol="aapl",
        analyzed_at=analyzed_at,
        price="230.00",
        currency="usd",
        title="가격 흐름은 균형",
        summary="새로운 중요 공시는 없고 가격 흐름은 제한적입니다.",
        short_term_stance="balanced",
        medium_term_stance="favorable",
        confidence="medium",
        change_label="initial",
        sections=(
            ReportSection(
                title="가격과 판단",
                items=(
                    ReportItem(
                        kind="fact",
                        text="현재가는 230달러입니다.",
                        source_keys=("quote",),
                    ),
                    ReportItem(
                        kind="inference",
                        text="단기 방향성은 뚜렷하지 않습니다.",
                        source_keys=("quote",),
                    ),
                ),
            ),
        ),
        snapshot={"price": "230.00", "changePercent": 0.5},
        sources=(source(),),
    )


def test_saves_and_loads_normalized_immutable_report(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)

    saved = repository.save_report(report(), now=datetime(2026, 9, 13, 1, 2, 3, tzinfo=UTC))

    assert saved.id > 0
    assert saved.symbol == "AAPL"
    assert saved.analyzed_at == "2026-09-13T00:00:00Z"
    assert saved.created_at == "2026-09-13T01:02:03Z"
    assert saved.price == "230"
    assert saved.currency == "USD"
    assert saved.title == "가격 흐름은 균형"
    assert saved.change_label == "initial"
    assert saved.sections[0].title == "가격과 판단"
    assert saved.sections[0].items[0].kind == "fact"
    assert saved.sources[0].position == 0
    assert saved.sources[0].retrieved_at == "2026-09-13T00:01:00Z"

    page = repository.list_reports("nasdaq", "aapl")
    assert page.items == (saved,)
    assert page.next_cursor is None


def test_run_id_is_idempotent_only_for_the_same_payload(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)

    first = repository.save_report(report(), now=datetime(2026, 9, 13, tzinfo=UTC))
    replay = repository.save_report(report(), now=datetime(2026, 9, 14, tzinfo=UTC))

    assert replay == first
    with pytest.raises(ReportConflict, match="run_id_payload_conflict"):
        repository.save_report(replace(report(), summary="다른 판단"))
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM investment_reports").fetchone()[0] == 1


def test_new_run_creates_a_new_report_instead_of_updating(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)

    first = repository.save_report(report("run-1"))
    second = repository.save_report(
        replace(
            report("run-2", "2026-09-13T10:00:00+09:00"),
            change_label="view_changed",
            short_term_stance="cautious",
        )
    )

    assert second.id != first.id
    assert repository.list_reports("NASDAQ", "AAPL").items == (second, first)


def test_latest_first_cursor_handles_equal_analysis_times(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)
    timestamp = "2026-09-13T09:00:00Z"
    saved = [repository.save_report(report(f"run-{index}", timestamp)) for index in range(3)]

    first_page = repository.list_reports("NASDAQ", "AAPL", limit=2)
    second_page = repository.list_reports("NASDAQ", "AAPL", limit=2, cursor=first_page.next_cursor)

    assert [item.id for item in first_page.items] == [saved[2].id, saved[1].id]
    assert first_page.next_cursor is not None
    assert second_page.items == (saved[0],)
    assert second_page.next_cursor is None


@pytest.mark.parametrize(
    ("changed", "code"),
    [
        ({"short_term_stance": "buy"}, "invalid_short_term_stance"),
        ({"medium_term_stance": "sell"}, "invalid_medium_term_stance"),
        ({"confidence": "certain"}, "invalid_confidence"),
        ({"change_label": "revised"}, "invalid_change_label"),
        ({"title": "x" * 201}, "invalid_title"),
        ({"price": "NaN"}, "invalid_price"),
        ({"currency": "US"}, "invalid_currency"),
    ],
)
def test_rejects_invalid_scalar_fields(tmp_path: Path, changed: dict[str, str], code: str) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)

    with pytest.raises(ReportValidationError, match=code):
        repository.save_report(replace(report(), **changed))


def test_rejects_unsafe_url_and_unreferenced_section_source(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)

    unsafe = replace(source(), url="javascript:alert(1)")
    with pytest.raises(ReportValidationError, match="invalid_source_url"):
        repository.save_report(replace(report(), sources=(unsafe,)))

    unsafe_key = replace(source(), source_key="bad key")
    with pytest.raises(ReportValidationError, match="invalid_source_key"):
        repository.save_report(replace(report(), sources=(unsafe_key,)))
    malformed = replace(source(), url="https://[")
    with pytest.raises(ReportValidationError, match="invalid_source_url"):
        repository.save_report(replace(report(), sources=(malformed,)))

    bad_section = ReportSection(
        title="근거",
        items=(ReportItem(kind="fact", text="근거 없음", source_keys=("missing",)),),
    )
    with pytest.raises(ReportValidationError, match="invalid_item_source_keys"):
        repository.save_report(replace(report(), sections=(bad_section,)))

    unsupported = ReportSection(
        title="근거 없는 주장",
        items=(ReportItem(kind="opinion", text="근거 없음", source_keys=()),),
    )
    with pytest.raises(ReportValidationError, match="report_item_requires_source"):
        repository.save_report(replace(report(), sections=(unsupported,)))

    no_market_data = replace(source(), kind="regulator")
    with pytest.raises(ReportValidationError, match="market_data_source_required"):
        repository.save_report(replace(report(), sources=(no_market_data,)))


def test_rejects_excessive_counts_and_invalid_cursor(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)

    too_many_sections = tuple(
        ReportSection(
            title=str(index),
            items=(ReportItem(kind="unknown", text="내용"),),
        )
        for index in range(13)
    )
    with pytest.raises(ReportValidationError, match="invalid_sections"):
        repository.save_report(replace(report(), sections=too_many_sections))
    with pytest.raises(ReportValidationError, match="invalid_sections"):
        repository.save_report(replace(report(), sections=()))
    with pytest.raises(ReportValidationError, match="invalid_sources"):
        repository.save_report(replace(report(), sources=()))
    empty_section = ReportSection(title="빈 섹션", items=())
    with pytest.raises(ReportValidationError, match="invalid_section_items"):
        repository.save_report(replace(report(), sections=(empty_section,)))
    too_many_items = ReportSection(
        title="너무 많은 항목",
        items=tuple(ReportItem(kind="unknown", text=str(index)) for index in range(13)),
    )
    with pytest.raises(ReportValidationError, match="invalid_section_items"):
        repository.save_report(replace(report(), sections=(too_many_items,)))
    invalid_kind = ReportSection(
        title="잘못된 분류",
        items=(ReportItem(kind="prediction", text="내용"),),
    )
    with pytest.raises(ReportValidationError, match="invalid_item_kind"):
        repository.save_report(replace(report(), sections=(invalid_kind,)))
    with pytest.raises(ReportValidationError, match="invalid_limit"):
        repository.list_reports("NASDAQ", "AAPL", limit=51)
    with pytest.raises(ReportValidationError, match="invalid_cursor"):
        repository.list_reports("NASDAQ", "AAPL", cursor="not-a-cursor")


def test_unknown_instrument_is_rejected_at_repository_boundary(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)

    with pytest.raises(ReportValidationError, match="instrument_not_found"):
        repository.save_report(replace(report(), symbol="MISSING"))


def test_opportunistic_retention_prunes_report_and_sources(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    repository = InvestmentReportRepository(path)
    repository.save_report(report("old"), now=datetime(2025, 1, 1, tzinfo=UTC))

    repository.save_report(
        report("new", "2026-01-02T00:00:00Z"),
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    with connect(path) as connection:
        assert (
            connection.execute("SELECT 1 FROM investment_reports WHERE run_id = 'old'").fetchone()
            is None
        )
        assert (
            connection.execute(
                """
            SELECT 1
            FROM report_sources AS s
            JOIN investment_reports AS r ON r.id = s.report_id
            WHERE r.run_id = 'old'
            """
            ).fetchone()
            is None
        )


def test_database_constraints_reject_invalid_taxonomy(tmp_path: Path) -> None:
    path = tmp_path / "stock.db"
    prepare_database(path)
    with connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO investment_reports (
                run_id, market, symbol, analyzed_at, created_at, price, currency,
                title, summary, short_term_stance, medium_term_stance, confidence,
                change_label, sections_json, snapshot_json
            ) VALUES (
                'bad', 'NASDAQ', 'AAPL', '2026-09-13T00:00:00Z',
                '2026-09-13T00:00:00Z', '230', 'USD', '제목', '요약',
                'buy', 'balanced', 'medium', 'initial', '[]', '{}'
            )
            """
        )
