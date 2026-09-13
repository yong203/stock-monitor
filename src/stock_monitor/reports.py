from __future__ import annotations

import base64
import binascii
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from .database import MARKETS, connect

STANCES = {"favorable", "balanced", "cautious", "insufficient"}
CONFIDENCES = {"high", "medium", "low"}
CHANGE_LABELS = {"initial", "view_changed", "view_reinforced", "facts_updated", "unchanged"}
SECTION_KINDS = {"fact", "inference", "opinion", "unknown"}
SOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MAX_SECTIONS = 12
MAX_ITEMS_PER_SECTION = 12
MAX_SOURCES = 20
MAX_JSON_BYTES = 32_000
MAX_PAGE_SIZE = 50
RETENTION_DAYS = 365


class ReportValidationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ReportConflict(Exception):
    pass


@dataclass(frozen=True)
class ReportItem:
    kind: str
    text: str
    source_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportSection:
    title: str
    items: tuple[ReportItem, ...]


@dataclass(frozen=True)
class ReportSourceInput:
    source_key: str
    kind: str
    title: str
    publisher: str
    url: str
    published_at: str | None
    retrieved_at: str


@dataclass(frozen=True)
class InvestmentReportInput:
    run_id: str
    market: str
    symbol: str
    analyzed_at: str
    price: str
    currency: str
    title: str
    summary: str
    short_term_stance: str
    medium_term_stance: str
    confidence: str
    change_label: str
    sections: tuple[ReportSection, ...]
    snapshot: Mapping[str, object]
    sources: tuple[ReportSourceInput, ...]


@dataclass(frozen=True)
class ReportSource:
    source_key: str
    position: int
    kind: str
    title: str
    publisher: str
    url: str
    published_at: str | None
    retrieved_at: str


@dataclass(frozen=True)
class InvestmentReport:
    id: int
    run_id: str
    market: str
    symbol: str
    analyzed_at: str
    created_at: str
    price: str
    currency: str
    title: str
    summary: str
    short_term_stance: str
    medium_term_stance: str
    confidence: str
    change_label: str
    sections: tuple[ReportSection, ...]
    snapshot: Mapping[str, object]
    sources: tuple[ReportSource, ...]


@dataclass(frozen=True)
class ReportPage:
    items: tuple[InvestmentReport, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class _NormalizedReport:
    run_id: str
    market: str
    symbol: str
    analyzed_at: str
    price: str
    currency: str
    title: str
    summary: str
    short_term_stance: str
    medium_term_stance: str
    confidence: str
    change_label: str
    sections_json: str
    snapshot_json: str
    sources: tuple[ReportSourceInput, ...]


class InvestmentReportRepository:
    def __init__(self, database_path: Path):
        self._database_path = database_path

    def save_report(
        self, report: InvestmentReportInput, *, now: datetime | None = None
    ) -> InvestmentReport:
        normalized = _normalize_report(report)
        reference = _aware_datetime(now or datetime.now(UTC), "now")
        created_at = _format_datetime(reference)
        cutoff = _format_datetime(reference - timedelta(days=RETENTION_DAYS))

        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM investment_reports WHERE created_at < ?", (cutoff,))
                existing = connection.execute(
                    "SELECT id FROM investment_reports WHERE run_id = ?", (normalized.run_id,)
                ).fetchone()
                if existing is not None:
                    saved = _load_report(connection, existing["id"])
                    if _payload(saved) != _normalized_payload(normalized):
                        raise ReportConflict("run_id_payload_conflict")
                    connection.commit()
                    return saved

                instrument = connection.execute(
                    "SELECT 1 FROM instruments WHERE market = ? AND symbol = ?",
                    (normalized.market, normalized.symbol),
                ).fetchone()
                if instrument is None:
                    raise ReportValidationError("instrument_not_found")

                cursor = connection.execute(
                    """
                    INSERT INTO investment_reports (
                        run_id, market, symbol, analyzed_at, created_at, price, currency,
                        title, summary, short_term_stance, medium_term_stance, confidence,
                        change_label, sections_json, snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized.run_id,
                        normalized.market,
                        normalized.symbol,
                        normalized.analyzed_at,
                        created_at,
                        normalized.price,
                        normalized.currency,
                        normalized.title,
                        normalized.summary,
                        normalized.short_term_stance,
                        normalized.medium_term_stance,
                        normalized.confidence,
                        normalized.change_label,
                        normalized.sections_json,
                        normalized.snapshot_json,
                    ),
                )
                report_id = cursor.lastrowid
                connection.executemany(
                    """
                    INSERT INTO report_sources (
                        report_id, source_key, position, kind, title, publisher, url,
                        published_at, retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            report_id,
                            source.source_key,
                            position,
                            source.kind,
                            source.title,
                            source.publisher,
                            source.url,
                            source.published_at,
                            source.retrieved_at,
                        )
                        for position, source in enumerate(normalized.sources)
                    ],
                )
                saved = _load_report(connection, report_id)
                connection.commit()
                return saved
            except Exception:
                connection.rollback()
                raise

    def list_reports(
        self,
        market: str,
        symbol: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ReportPage:
        normalized_market = _choice(_required_text(market, "market", 16).upper(), MARKETS, "market")
        normalized_symbol = _required_text(symbol, "symbol", 64).upper()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ReportValidationError("invalid_limit")
        before = _decode_cursor(cursor) if cursor is not None else None

        parameters: list[object] = [normalized_market, normalized_symbol]
        condition = ""
        if before is not None:
            condition = "AND (analyzed_at < ? OR (analyzed_at = ? AND id < ?))"
            parameters.extend((before[0], before[0], before[1]))
        parameters.append(limit + 1)

        with connect(self._database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT id
                FROM investment_reports
                WHERE market = ? AND symbol = ? {condition}
                ORDER BY analyzed_at DESC, id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            items = tuple(_load_report(connection, row["id"]) for row in rows[:limit])

        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.analyzed_at, last.id)
        return ReportPage(items=items, next_cursor=next_cursor)

    def prune_expired(self, *, now: datetime | None = None) -> int:
        reference = _aware_datetime(now or datetime.now(UTC), "now")
        cutoff = _format_datetime(reference - timedelta(days=RETENTION_DAYS))
        with connect(self._database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM investment_reports WHERE created_at < ?", (cutoff,)
                )
                connection.commit()
                return cursor.rowcount
            except Exception:
                connection.rollback()
                raise


def _normalize_report(report: InvestmentReportInput) -> _NormalizedReport:
    if not isinstance(report, InvestmentReportInput):
        raise ReportValidationError("invalid_report")
    run_id = _required_text(report.run_id, "run_id", 128)
    market = _choice(_required_text(report.market, "market", 16).upper(), MARKETS, "market")
    symbol = _required_text(report.symbol, "symbol", 64).upper()
    analyzed_at = _timestamp(report.analyzed_at, "analyzed_at")
    price = _price(report.price)
    currency = _required_text(report.currency, "currency", 8).upper()
    if len(currency) != 3 or not currency.isalpha() or not currency.isascii():
        raise ReportValidationError("invalid_currency")
    title = _required_text(report.title, "title", 200)
    summary = _required_text(report.summary, "summary", 5_000)
    short_term_stance = _choice(report.short_term_stance, STANCES, "short_term_stance")
    medium_term_stance = _choice(report.medium_term_stance, STANCES, "medium_term_stance")
    confidence = _choice(report.confidence, CONFIDENCES, "confidence")
    change_label = _choice(report.change_label, CHANGE_LABELS, "change_label")

    if not isinstance(report.sections, tuple) or not 1 <= len(report.sections) <= MAX_SECTIONS:
        raise ReportValidationError("invalid_sections")
    if not isinstance(report.sources, tuple) or not 1 <= len(report.sources) <= MAX_SOURCES:
        raise ReportValidationError("invalid_sources")

    sources = tuple(_normalize_source(source) for source in report.sources)
    source_keys = [source.source_key for source in sources]
    if len(source_keys) != len(set(source_keys)):
        raise ReportValidationError("duplicate_source_key")

    sections: list[dict[str, object]] = []
    available_keys = set(source_keys)
    market_data_keys = {source.source_key for source in sources if source.kind == "market_data"}
    linked_keys: set[str] = set()
    for section in report.sections:
        if not isinstance(section, ReportSection):
            raise ReportValidationError("invalid_section")
        section_title = _required_text(section.title, "section_title", 120)
        if (
            not isinstance(section.items, tuple)
            or not section.items
            or len(section.items) > MAX_ITEMS_PER_SECTION
        ):
            raise ReportValidationError("invalid_section_items")
        items: list[dict[str, object]] = []
        for item in section.items:
            if not isinstance(item, ReportItem):
                raise ReportValidationError("invalid_report_item")
            kind = _choice(item.kind, SECTION_KINDS, "item_kind")
            text = _required_text(item.text, "item_text", 4_000)
            if not isinstance(item.source_keys, tuple) or len(item.source_keys) > MAX_SOURCES:
                raise ReportValidationError("invalid_item_source_keys")
            keys = tuple(_required_text(key, "item_source_key", 128) for key in item.source_keys)
            if len(keys) != len(set(keys)) or not set(keys) <= available_keys:
                raise ReportValidationError("invalid_item_source_keys")
            if kind != "unknown" and not keys:
                raise ReportValidationError("report_item_requires_source")
            linked_keys.update(keys)
            items.append({"kind": kind, "text": text, "source_keys": list(keys)})
        sections.append({"title": section_title, "items": items})

    if not market_data_keys & linked_keys:
        raise ReportValidationError("market_data_source_required")

    if not isinstance(report.snapshot, Mapping):
        raise ReportValidationError("invalid_snapshot")
    snapshot = _json_value(dict(report.snapshot), depth=0)
    sections_json = _json(sections, "sections")
    snapshot_json = _json(snapshot, "snapshot")
    return _NormalizedReport(
        run_id=run_id,
        market=market,
        symbol=symbol,
        analyzed_at=analyzed_at,
        price=price,
        currency=currency,
        title=title,
        summary=summary,
        short_term_stance=short_term_stance,
        medium_term_stance=medium_term_stance,
        confidence=confidence,
        change_label=change_label,
        sections_json=sections_json,
        snapshot_json=snapshot_json,
        sources=sources,
    )


def _normalize_source(source: ReportSourceInput) -> ReportSourceInput:
    if not isinstance(source, ReportSourceInput):
        raise ReportValidationError("invalid_source")
    url = _required_text(source.url, "source_url", 2_048)
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise ReportValidationError("invalid_source_url") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None:
        raise ReportValidationError("invalid_source_url")
    source_key = _required_text(source.source_key, "source_key", 128)
    if SOURCE_KEY.fullmatch(source_key) is None:
        raise ReportValidationError("invalid_source_key")
    return ReportSourceInput(
        source_key=source_key,
        kind=_required_text(source.kind, "source_kind", 32),
        title=_required_text(source.title, "source_title", 300),
        publisher=_required_text(source.publisher, "source_publisher", 120),
        url=url,
        published_at=(
            _timestamp(source.published_at, "source_published_at")
            if source.published_at is not None
            else None
        ),
        retrieved_at=_timestamp(source.retrieved_at, "source_retrieved_at"),
    )


def _load_report(connection: sqlite3.Connection, report_id: int) -> InvestmentReport:
    row = connection.execute(
        "SELECT * FROM investment_reports WHERE id = ?", (report_id,)
    ).fetchone()
    sources = connection.execute(
        "SELECT * FROM report_sources WHERE report_id = ? ORDER BY position", (report_id,)
    ).fetchall()
    return InvestmentReport(
        id=row["id"],
        run_id=row["run_id"],
        market=row["market"],
        symbol=row["symbol"],
        analyzed_at=row["analyzed_at"],
        created_at=row["created_at"],
        price=row["price"],
        currency=row["currency"],
        title=row["title"],
        summary=row["summary"],
        short_term_stance=row["short_term_stance"],
        medium_term_stance=row["medium_term_stance"],
        confidence=row["confidence"],
        change_label=row["change_label"],
        sections=tuple(
            ReportSection(
                title=section["title"],
                items=tuple(
                    ReportItem(
                        kind=item["kind"],
                        text=item["text"],
                        source_keys=tuple(item["source_keys"]),
                    )
                    for item in section["items"]
                ),
            )
            for section in json.loads(row["sections_json"])
        ),
        snapshot=json.loads(row["snapshot_json"]),
        sources=tuple(
            ReportSource(
                source_key=source["source_key"],
                position=source["position"],
                kind=source["kind"],
                title=source["title"],
                publisher=source["publisher"],
                url=source["url"],
                published_at=source["published_at"],
                retrieved_at=source["retrieved_at"],
            )
            for source in sources
        ),
    )


def _payload(report: InvestmentReport) -> tuple[object, ...]:
    return (
        report.run_id,
        report.market,
        report.symbol,
        report.analyzed_at,
        report.price,
        report.currency,
        report.title,
        report.summary,
        report.short_term_stance,
        report.medium_term_stance,
        report.confidence,
        report.change_label,
        _json(
            [
                {
                    "title": section.title,
                    "items": [
                        {
                            "kind": item.kind,
                            "text": item.text,
                            "source_keys": list(item.source_keys),
                        }
                        for item in section.items
                    ],
                }
                for section in report.sections
            ],
            "sections",
        ),
        _json(report.snapshot, "snapshot"),
        tuple(
            (
                source.source_key,
                source.kind,
                source.title,
                source.publisher,
                source.url,
                source.published_at,
                source.retrieved_at,
            )
            for source in report.sources
        ),
    )


def _normalized_payload(report: _NormalizedReport) -> tuple[object, ...]:
    return (
        report.run_id,
        report.market,
        report.symbol,
        report.analyzed_at,
        report.price,
        report.currency,
        report.title,
        report.summary,
        report.short_term_stance,
        report.medium_term_stance,
        report.confidence,
        report.change_label,
        report.sections_json,
        report.snapshot_json,
        tuple(
            (
                source.source_key,
                source.kind,
                source.title,
                source.publisher,
                source.url,
                source.published_at,
                source.retrieved_at,
            )
            for source in report.sources
        ),
    )


def _required_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ReportValidationError(f"invalid_{field}")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ReportValidationError(f"invalid_{field}")
    return normalized


def _choice(value: object, choices: set[str] | tuple[str, ...], field: str) -> str:
    normalized = _required_text(value, field, 32)
    if normalized not in choices:
        raise ReportValidationError(f"invalid_{field}")
    return normalized


def _price(value: object) -> str:
    raw = _required_text(value, "price", 32)
    try:
        decimal = Decimal(raw)
    except InvalidOperation as error:
        raise ReportValidationError("invalid_price") from error
    if not decimal.is_finite() or decimal < 0:
        raise ReportValidationError("invalid_price")
    normalized = format(decimal, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _timestamp(value: object, field: str) -> str:
    raw = _required_text(value, field, 40)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReportValidationError(f"invalid_{field}") from error
    return _format_datetime(_aware_datetime(parsed, field))


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReportValidationError(f"invalid_{field}")
    return value.astimezone(UTC)


def _format_datetime(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: object, field: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > MAX_JSON_BYTES:
        raise ReportValidationError(f"invalid_{field}")
    return encoded


def _json_value(value: object, *, depth: int) -> object:
    if depth > 6:
        raise ReportValidationError("invalid_snapshot")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReportValidationError("invalid_snapshot")
        return value
    if isinstance(value, str):
        if len(value) > 4_000 or "\x00" in value:
            raise ReportValidationError("invalid_snapshot")
        return value
    if isinstance(value, Mapping):
        if len(value) > 50:
            raise ReportValidationError("invalid_snapshot")
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = _required_text(key, "snapshot_key", 64)
            result[normalized_key] = _json_value(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 50:
            raise ReportValidationError("invalid_snapshot")
        return [_json_value(item, depth=depth + 1) for item in value]
    raise ReportValidationError("invalid_snapshot")


def _encode_cursor(analyzed_at: str, report_id: int) -> str:
    value = json.dumps([analyzed_at, report_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    raw = _required_text(cursor, "cursor", 256)
    try:
        padding = "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as error:
        raise ReportValidationError("invalid_cursor") from error
    if (
        not isinstance(value, list)
        or len(value) != 2
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
        or value[1] <= 0
    ):
        raise ReportValidationError("invalid_cursor")
    return _timestamp(value[0], "cursor"), value[1]
