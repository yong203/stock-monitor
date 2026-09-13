from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from stock_monitor.app import create_app


@dataclass(frozen=True)
class FakeSubscription:
    queue: asyncio.Queue[dict[str, object]]


class FakeService:
    def __init__(self) -> None:
        self.unsubscribed = 0

    async def subscribe(self) -> FakeSubscription:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        queue.put_nowait(
            {
                "revision": 7,
                "connection": {"status": "connected", "error": None},
                "markets": {
                    "KR": {
                        "status": "ready",
                        "phase": "holiday",
                        "session": None,
                        "next_event": None,
                    }
                },
                "quotes": [],
            }
        )
        return FakeSubscription(queue)

    async def unsubscribe(self, subscription: FakeSubscription) -> None:
        self.unsubscribed += 1


class FakeRuntime:
    def __init__(self) -> None:
        self.service = FakeService()

    async def start(self, database_path: Path) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def refresh_watchlist(self, database_path: Path) -> None:
        pass


def test_sse_starts_with_latest_full_snapshot_and_cleans_up(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    app = create_app(
        tmp_path / "stock.db",
        enable_live=True,
        runtime_builder=lambda _: runtime,  # type: ignore[arg-type,return-value]
    )

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            route = next(route for route in app.routes if route.path == "/api/quotes/stream")
            response = await route.endpoint()
            event = await anext(response.body_iterator)
            assert event.startswith("id: 7\n")
            assert '"status":"connected"' in event
            assert '"phase":"holiday"' in event
            assert '"quotes":[]' in event
            await response.body_iterator.aclose()

    asyncio.run(scenario())
    assert runtime.service.unsubscribed == 1
