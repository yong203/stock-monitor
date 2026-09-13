from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, conint, conlist, constr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .analysis import build_toss_analysis
from .database import initialize_database
from .market_data import MarketDataError
from .reports import (
    InvestmentReport,
    InvestmentReportInput,
    InvestmentReportRepository,
    ReportConflict,
    ReportItem,
    ReportSection,
    ReportSourceInput,
    ReportValidationError,
)
from .runtime import MarketDataRuntime, build_market_data_runtime
from .settings import (
    CredentialsError,
    UnsafeCredentialsError,
    credentials_path,
    database_path,
    load_credentials,
)
from .watchlist import (
    DuplicateWatchlistItem,
    Instrument,
    InstrumentNotFound,
    InvalidWatchlistOrder,
    WatchlistFull,
    WatchlistItem,
    WatchlistRepository,
)

PACKAGE_ROOT = Path(__file__).parent
MAX_REPORT_BODY_BYTES = 256 * 1024
templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")


class _ReportBodyTooLarge(Exception):
    pass


class _LimitReportBodyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_report_write = (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == "/internal/v1/reports"
        )
        if not is_report_write:
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for key, value in scope["headers"] if key.lower() == b"content-length"), None
        )
        try:
            declared_size = int(content_length) if content_length is not None else None
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > MAX_REPORT_BODY_BYTES:
            await _report_body_too_large_response(scope, receive, send)
            return

        received_size = 0

        async def limited_receive() -> Message:
            nonlocal received_size
            message = await receive()
            if message["type"] == "http.request":
                received_size += len(message.get("body", b""))
                if received_size > MAX_REPORT_BODY_BYTES:
                    raise _ReportBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _ReportBodyTooLarge:
            await _report_body_too_large_response(scope, receive, send)


async def _report_body_too_large_response(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": {"code": "report_body_too_large"}},
    )
    await response(scope, receive, send)


class AddWatchlistRequest(BaseModel):
    market: constr(strict=True, min_length=1, max_length=16)
    symbol: constr(strict=True, min_length=1, max_length=64)

    class Config:
        extra = "forbid"


class ReorderWatchlistRequest(BaseModel):
    item_ids: conlist(conint(strict=True, gt=0), max_items=20)

    class Config:
        extra = "forbid"


class ReportItemRequest(BaseModel):
    kind: Literal["fact", "inference", "opinion", "unknown"]
    text: constr(strict=True, min_length=1, max_length=4_000)
    source_keys: conlist(constr(strict=True, min_length=1, max_length=128), max_items=20) = Field(
        default_factory=list
    )

    class Config:
        extra = "forbid"


class ReportSectionRequest(BaseModel):
    title: constr(strict=True, min_length=1, max_length=120)
    items: conlist(ReportItemRequest, min_items=1, max_items=12)

    class Config:
        extra = "forbid"


class ReportSourceRequest(BaseModel):
    source_key: constr(
        strict=True, min_length=1, max_length=128, regex=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    kind: constr(strict=True, min_length=1, max_length=32)
    title: constr(strict=True, min_length=1, max_length=300)
    publisher: constr(strict=True, min_length=1, max_length=120)
    url: constr(strict=True, min_length=1, max_length=2_048)
    published_at: constr(strict=True, min_length=1, max_length=64) | None = None
    retrieved_at: constr(strict=True, min_length=1, max_length=64)

    class Config:
        extra = "forbid"


class CreateReportRequest(BaseModel):
    run_id: UUID
    market: constr(strict=True, min_length=1, max_length=16)
    symbol: constr(strict=True, min_length=1, max_length=64)
    analyzed_at: constr(strict=True, min_length=1, max_length=64)
    price: constr(strict=True, min_length=1, max_length=64)
    currency: constr(strict=True, min_length=3, max_length=8)
    title: constr(strict=True, min_length=1, max_length=200)
    summary: constr(strict=True, min_length=1, max_length=5_000)
    short_term_stance: Literal["favorable", "balanced", "cautious", "insufficient"]
    medium_term_stance: Literal["favorable", "balanced", "cautious", "insufficient"]
    confidence: Literal["high", "medium", "low"]
    change_label: Literal[
        "initial", "view_changed", "view_reinforced", "facts_updated", "unchanged"
    ]
    sections: conlist(ReportSectionRequest, min_items=1, max_items=12)
    sources: conlist(ReportSourceRequest, min_items=1, max_items=20)
    snapshot: dict[str, object]

    class Config:
        extra = "forbid"


ReportAnalysisBuilder = Callable[[object, WatchlistItem], Awaitable[dict[str, object]]]


def create_app(
    database_file: Path | None = None,
    *,
    enable_live: bool = False,
    runtime_builder: Callable[[Path], MarketDataRuntime | None] = build_market_data_runtime,
    report_writer_token_loader: Callable[[], str | None] | None = None,
    report_analysis_builder: ReportAnalysisBuilder = build_toss_analysis,
) -> FastAPI:
    selected_database = database_file or database_path()
    runtime: MarketDataRuntime | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal runtime
        initialize_database(selected_database)
        if enable_live:
            runtime = runtime_builder(selected_database)
            if runtime is not None:
                await runtime.start(selected_database)
        app.state.market_data_runtime = runtime
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.stop()

    app = FastAPI(
        title="Stock Monitor",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(_LimitReportBodyMiddleware)
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    repository = WatchlistRepository(selected_database)
    report_repository = InvestmentReportRepository(selected_database)
    token_loader = report_writer_token_loader or _load_report_writer_token

    def require_report_writer(authorization: str | None = Header(default=None)) -> None:
        expected = token_loader()
        if expected is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "report_writer_not_configured"},
            )
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail={"code": "report_writer_unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"items": repository.list_items(), "page": "dashboard"},
        )

    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="watchlist.html",
            context={"items": repository.list_items(), "page": "watchlist"},
        )

    @app.get("/instruments/{market}/{symbol}", response_class=HTMLResponse)
    def instrument_page(request: Request, market: str, symbol: str) -> HTMLResponse:
        target = _watchlist_target(repository, market, symbol)
        if target is None:
            raise HTTPException(status_code=404, detail={"code": "watchlist_item_not_found"})
        report_repository.prune_expired()
        report_page = report_repository.list_reports(target.market, target.symbol, limit=20)
        return templates.TemplateResponse(
            request=request,
            name="instrument.html",
            context={
                "instrument": target,
                "reports": report_page.items,
                "next_cursor": report_page.next_cursor,
                "page": "instrument",
            },
        )

    @app.get("/api/instruments/{market}/{symbol}/reports", response_class=HTMLResponse)
    def older_reports(
        request: Request,
        market: str,
        symbol: str,
        cursor: str = Query(min_length=1, max_length=256),
    ) -> HTMLResponse:
        target = _watchlist_target(repository, market, symbol)
        if target is None:
            raise HTTPException(status_code=404, detail={"code": "watchlist_item_not_found"})
        report_repository.prune_expired()
        try:
            report_page = report_repository.list_reports(
                target.market, target.symbol, limit=20, cursor=cursor
            )
        except ReportValidationError as error:
            raise HTTPException(status_code=400, detail={"code": error.code}) from error
        return templates.TemplateResponse(
            request=request,
            name="_report_page.html",
            context={"reports": report_page.items},
            headers={
                "Cache-Control": "no-store",
                "X-Next-Cursor": report_page.next_cursor or "",
            },
        )

    @app.get("/api/instruments/search")
    def search_instruments(q: str = Query(min_length=1, max_length=80)) -> dict[str, object]:
        if not repository.catalog_ready():
            raise HTTPException(
                status_code=503,
                detail={"code": "stock_catalog_not_ready"},
            )
        return {"items": [_instrument_json(item) for item in repository.search_instruments(q)]}

    @app.get("/api/watchlist")
    def list_watchlist() -> dict[str, object]:
        return {"items": [_watchlist_json(item) for item in repository.list_items()]}

    @app.get("/internal/v1/report-targets", dependencies=[Depends(require_report_writer)])
    def report_targets() -> dict[str, object]:
        return {"targets": [_instrument_json(item) for item in repository.list_items()]}

    @app.get(
        "/internal/v1/report-context/{market}/{symbol}",
        dependencies=[Depends(require_report_writer)],
    )
    async def report_context(market: str, symbol: str) -> dict[str, object]:
        target = await asyncio.to_thread(_watchlist_target, repository, market, symbol)
        if target is None:
            raise HTTPException(status_code=404, detail={"code": "report_target_not_found"})
        if runtime is None:
            raise HTTPException(status_code=503, detail={"code": "market_data_not_configured"})
        try:
            analysis = await report_analysis_builder(runtime.client, target)
        except MarketDataError as error:
            raise HTTPException(status_code=503, detail={"code": error.code}) from error
        quote = analysis.get("quote")
        analytics = {key: value for key, value in analysis.items() if key != "quote"}
        await asyncio.to_thread(report_repository.prune_expired)
        latest = await asyncio.to_thread(
            report_repository.list_reports, target.market, target.symbol, limit=1
        )
        return {
            "target": _instrument_json(target),
            "quote": quote,
            "analytics": analytics,
            "latest_report": _report_json(latest.items[0]) if latest.items else None,
        }

    @app.post(
        "/internal/v1/reports",
        status_code=201,
        dependencies=[Depends(require_report_writer)],
    )
    async def create_report(payload: CreateReportRequest) -> dict[str, object]:
        target = await asyncio.to_thread(
            _watchlist_target, repository, payload.market, payload.symbol
        )
        if target is None:
            raise HTTPException(status_code=404, detail={"code": "report_target_not_found"})
        try:
            saved = await asyncio.to_thread(
                report_repository.save_report,
                _report_input(payload),
            )
        except ReportConflict as error:
            raise HTTPException(
                status_code=409, detail={"code": "report_run_id_conflict"}
            ) from error
        except ReportValidationError as error:
            raise HTTPException(status_code=422, detail={"code": error.code}) from error
        return {"report": _report_json(saved)}

    @app.post("/api/watchlist", status_code=201)
    async def add_watchlist(payload: AddWatchlistRequest) -> dict[str, object]:
        if not repository.catalog_ready():
            raise HTTPException(
                status_code=503,
                detail={"code": "stock_catalog_not_ready"},
            )
        try:
            item = await asyncio.to_thread(repository.add_item, payload.market, payload.symbol)
        except InstrumentNotFound as error:
            raise HTTPException(status_code=404, detail={"code": "instrument_not_found"}) from error
        except DuplicateWatchlistItem as error:
            raise HTTPException(status_code=409, detail={"code": "watchlist_duplicate"}) from error
        except WatchlistFull as error:
            raise HTTPException(status_code=409, detail={"code": "watchlist_full"}) from error
        await _refresh_runtime(runtime, selected_database)
        return _watchlist_json(item)

    @app.delete("/api/watchlist/{item_id}", status_code=204)
    async def remove_watchlist(item_id: int) -> None:
        if not await asyncio.to_thread(repository.remove_item, item_id):
            raise HTTPException(status_code=404, detail={"code": "watchlist_item_not_found"})
        await _refresh_runtime(runtime, selected_database)

    @app.put("/api/watchlist/order")
    def reorder_watchlist(payload: ReorderWatchlistRequest) -> dict[str, object]:
        try:
            items = repository.reorder_items(payload.item_ids)
        except InvalidWatchlistOrder as error:
            raise HTTPException(
                status_code=409, detail={"code": "watchlist_order_conflict"}
            ) from error
        return {"items": [_watchlist_json(item) for item in items]}

    @app.get("/api/quotes/stream")
    async def quote_stream() -> StreamingResponse:
        if runtime is None:
            raise HTTPException(status_code=503, detail={"code": "market_data_not_configured"})

        async def events() -> AsyncIterator[str]:
            subscription = await runtime.service.subscribe()
            try:
                while True:
                    try:
                        snapshot = await asyncio.wait_for(subscription.queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {snapshot['revision']}\ndata: {payload}\n\n"
            finally:
                await runtime.service.unsubscribe(subscription)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/health/market-data")
    async def market_data_health() -> JSONResponse:
        if runtime is None:
            return JSONResponse(
                status_code=503,
                content={"status": "configuration_error", "error": "toss_not_configured"},
            )
        health = await runtime.service.health()
        status_code = (
            200
            if health["status"] in {"idle", "connected"} and health.get("calendar_status") == "ok"
            else 503
        )
        return JSONResponse(status_code=status_code, content=health)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/configuration")
    def configuration() -> JSONResponse:
        try:
            load_credentials(credentials_path())
        except UnsafeCredentialsError as error:
            return JSONResponse(
                status_code=503,
                content={"status": "invalid", "reason": error.code},
            )
        except CredentialsError:
            return JSONResponse(
                status_code=503,
                content={"status": "missing", "reason": "toss_not_configured"},
            )
        return JSONResponse(content={"status": "configured", "toss": "not_checked"})

    return app


def _instrument_json(item: Instrument) -> dict[str, str | bool]:
    return {
        "market": item.market,
        "symbol": item.symbol,
        "name": item.name,
        "security_type": item.security_type,
        "country": item.country,
        "is_etf": item.is_etf,
    }


def _watchlist_json(item: WatchlistItem) -> dict[str, str | int | bool]:
    return {
        "id": item.id,
        "position": item.position,
        **_instrument_json(item),
    }


def _watchlist_target(
    repository: WatchlistRepository, market: str, symbol: str
) -> WatchlistItem | None:
    normalized = (market.strip().upper(), symbol.strip().upper())
    return next(
        (
            item
            for item in repository.list_items()
            if (item.market, item.symbol.upper()) == normalized
        ),
        None,
    )


def _report_input(payload: CreateReportRequest) -> InvestmentReportInput:
    return InvestmentReportInput(
        run_id=str(payload.run_id),
        market=payload.market,
        symbol=payload.symbol,
        analyzed_at=payload.analyzed_at,
        price=payload.price,
        currency=payload.currency,
        title=payload.title,
        summary=payload.summary,
        short_term_stance=payload.short_term_stance,
        medium_term_stance=payload.medium_term_stance,
        confidence=payload.confidence,
        change_label=payload.change_label,
        sections=tuple(
            ReportSection(
                title=section.title,
                items=tuple(
                    ReportItem(
                        kind=item.kind,
                        text=item.text,
                        source_keys=tuple(item.source_keys),
                    )
                    for item in section.items
                ),
            )
            for section in payload.sections
        ),
        snapshot=payload.snapshot,
        sources=tuple(
            ReportSourceInput(
                source_key=source.source_key,
                kind=source.kind,
                title=source.title,
                publisher=source.publisher,
                url=source.url,
                published_at=source.published_at,
                retrieved_at=source.retrieved_at,
            )
            for source in payload.sources
        ),
    )


def _report_json(report: InvestmentReport) -> dict[str, object]:
    return {
        "id": report.id,
        "market": report.market,
        "symbol": report.symbol,
        "analyzed_at": report.analyzed_at,
        "created_at": report.created_at,
        "price": report.price,
        "currency": report.currency,
        "title": report.title,
        "summary": report.summary,
        "short_term_stance": report.short_term_stance,
        "medium_term_stance": report.medium_term_stance,
        "confidence": report.confidence,
        "change_label": report.change_label,
        "snapshot": dict(report.snapshot),
        "sections": [
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
        "sources": [
            {
                "source_key": source.source_key,
                "kind": source.kind,
                "title": source.title,
                "publisher": source.publisher,
                "url": source.url,
                "published_at": source.published_at,
                "retrieved_at": source.retrieved_at,
            }
            for source in report.sources
        ],
    }


def _load_report_writer_token() -> str | None:
    try:
        return load_credentials(credentials_path()).report_writer_token
    except CredentialsError:
        return None


async def _refresh_runtime(runtime: MarketDataRuntime | None, database_file: Path) -> None:
    if runtime is not None:
        await runtime.refresh_watchlist(database_file)


app = create_app(enable_live=True)
