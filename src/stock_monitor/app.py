from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, conint, conlist, constr

from .database import initialize_database
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
templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")


class AddWatchlistRequest(BaseModel):
    market: constr(strict=True, min_length=1, max_length=16)
    symbol: constr(strict=True, min_length=1, max_length=64)

    class Config:
        extra = "forbid"


class ReorderWatchlistRequest(BaseModel):
    item_ids: conlist(conint(strict=True, gt=0), max_items=20)

    class Config:
        extra = "forbid"


def create_app(
    database_file: Path | None = None,
    *,
    enable_live: bool = False,
    runtime_builder: Callable[[Path], MarketDataRuntime | None] = build_market_data_runtime,
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
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    repository = WatchlistRepository(selected_database)

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
        status_code = 200 if health["status"] in {"idle", "connected"} else 503
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


async def _refresh_runtime(runtime: MarketDataRuntime | None, database_file: Path) -> None:
    if runtime is not None:
        await runtime.refresh_watchlist(database_file)


app = create_app(enable_live=True)
