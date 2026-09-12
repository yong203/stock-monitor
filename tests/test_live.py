from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from stock_monitor.live import (
    AccessToken,
    LiveQuoteService,
    QuoteKey,
    QuoteUpdate,
)


class FakeTokenProvider:
    def __init__(self) -> None:
        self.token = AccessToken("secret-token", 1)
        self.invalidated: list[int] = []

    async def get_token(self) -> AccessToken:
        return self.token

    async def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)


class ControlledSnapshotProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[QuoteKey, ...]] = []
        self.responses: asyncio.Queue[Mapping[QuoteKey, QuoteUpdate]] = asyncio.Queue()

    async def fetch(
        self, token: AccessToken, keys: tuple[QuoteKey, ...]
    ) -> Mapping[QuoteKey, QuoteUpdate]:
        assert token.value == "secret-token"
        self.calls.append(keys)
        return await self.responses.get()


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | bytes | Exception] = asyncio.Queue()
        self.sent: asyncio.Queue[str] = asyncio.Queue()
        self.closed = 0

    async def send(self, message: str) -> None:
        await self.sent.put(message)

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed += 1


class FakeConnector:
    def __init__(self, *connections: FakeWebSocket | Exception) -> None:
        self.connections: asyncio.Queue[FakeWebSocket | Exception] = asyncio.Queue()
        for connection in connections:
            self.connections.put_nowait(connection)
        self.tokens: list[str] = []

    async def connect(self, access_token: str) -> FakeWebSocket:
        self.tokens.append(access_token)
        connection = await self.connections.get()
        if isinstance(connection, Exception):
            raise connection
        return connection


def run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


async def next_declaration(socket: FakeWebSocket) -> tuple[str, list[dict[str, object]]]:
    raw = await asyncio.wait_for(socket.sent.get(), timeout=1)
    declaration = json.loads(raw)
    request_id = declaration[0]["id"]
    return request_id, declaration


def ack(request_id: str, subscribed: list[str], rejected: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "type": "subscriptions",
            "id": request_id,
            "subscribed": subscribed,
            "rejected": rejected,
        }
    )


def trade(key: QuoteKey, price: str, currency: str = "USD") -> str:
    return json.dumps(
        {
            "type": "message",
            "topic": key.topic,
            "data": {
                "price": price,
                "volume": "1",
                "timestamp": "2026-09-12T09:30:00+09:00",
                "currency": currency,
            },
        }
    )


def test_declares_full_watchlist_and_handles_partial_ack_and_trade() -> None:
    async def scenario() -> None:
        token = FakeTokenProvider()
        snapshots = ControlledSnapshotProvider()
        socket = FakeWebSocket()
        service = LiveQuoteService(token, snapshots, FakeConnector(socket))
        samsung = QuoteKey("KR", "005930")
        apple = QuoteKey("us", "aapl")
        await service.set_desired([samsung, apple])
        await service.start()

        request_id, declaration = await next_declaration(socket)
        assert declaration == [
            {"id": request_id},
            {"type": "trade:kr", "codes": ["005930"]},
            {"type": "trade:us", "codes": ["AAPL"]},
        ]
        await socket.incoming.put(
            ack(
                request_id,
                [samsung.topic],
                [{"target": apple.topic, "code": "stock-not-found", "message": "ignored"}],
            )
        )
        await socket.incoming.put(trade(samsung, "72000", "KRW"))
        await _wait_until(
            lambda: service._quotes[samsung].price == Decimal("72000")  # noqa: SLF001
        )

        state = await service.snapshot()
        items = {item["symbol"]: item for item in state["quotes"]}
        assert items["005930"]["price"] == "72000"
        assert items["005930"]["status"] == "live"
        assert items["AAPL"]["status"] == "unavailable"
        assert items["AAPL"]["error"] == "stock-not-found"
        health = await service.health()
        assert health["status"] == "connected"
        assert health["subscribed"] == 1
        assert "secret-token" not in repr(token.token)
        assert "secret-token" not in json.dumps({"state": state, "health": health})

        await service.stop()
        assert socket.closed >= 1

    run(scenario())


def test_slow_subscriber_keeps_only_latest_full_snapshot() -> None:
    async def scenario() -> None:
        service = LiveQuoteService(
            FakeTokenProvider(), ControlledSnapshotProvider(), FakeConnector()
        )
        subscription = await service.subscribe()
        await service.set_desired([QuoteKey("us", "AAPL")])
        await service.set_desired([QuoteKey("us", "MSFT")])

        assert subscription.queue.qsize() == 1
        latest = subscription.queue.get_nowait()
        assert [item["symbol"] for item in latest["quotes"]] == ["MSFT"]
        await service.unsubscribe(subscription)
        assert (await service.health())["subscribers"] == 0

    run(scenario())


def test_websocket_tick_wins_over_late_rest_snapshot() -> None:
    async def scenario() -> None:
        snapshots = ControlledSnapshotProvider()
        socket = FakeWebSocket()
        service = LiveQuoteService(
            FakeTokenProvider(), snapshots, FakeConnector(socket), monotonic=lambda: 10.0
        )
        apple = QuoteKey("us", "AAPL")
        await service.set_desired([apple])
        await service.start()
        request_id, _ = await next_declaration(socket)
        await socket.incoming.put(ack(request_id, [apple.topic], []))
        await socket.incoming.put(trade(apple, "101"))
        await _wait_until(
            lambda: service._quotes[apple].price == Decimal("101")  # noqa: SLF001
        )
        await snapshots.responses.put(
            {
                apple: QuoteUpdate(
                    Decimal("100"),
                    "USD",
                    "2026-09-12T09:29:59+09:00",
                    Decimal("90"),
                    "2026-09-11",
                )
            }
        )
        await asyncio.sleep(0.01)

        item = (await service.snapshot())["quotes"][0]
        assert item["price"] == "101"
        assert item["previous_close"] == "90"
        assert item["baseline_date"] == "2026-09-11"
        assert item["change"] == "11"
        await service.stop()

    run(scenario())


def test_periodic_snapshot_replaces_next_day_baseline_without_overwriting_new_tick() -> None:
    async def scenario() -> None:
        snapshots = ControlledSnapshotProvider()
        socket = FakeWebSocket()
        service = LiveQuoteService(
            FakeTokenProvider(),
            snapshots,
            FakeConnector(socket),
            snapshot_interval=0.01,
        )
        apple = QuoteKey("us", "AAPL")
        await service.set_desired([apple])
        await service.start()
        request_id, _ = await next_declaration(socket)
        await socket.incoming.put(ack(request_id, [apple.topic], []))
        await snapshots.responses.put(
            {apple: QuoteUpdate(Decimal("100"), "USD", None, Decimal("90"), "2026-09-11")}
        )
        await _wait_until(
            lambda: service._quotes[apple].baseline_date == "2026-09-11"  # noqa: SLF001
        )

        await _wait_until(lambda: len(snapshots.calls) >= 2)
        await socket.incoming.put(trade(apple, "101"))
        await _wait_until(
            lambda: service._quotes[apple].price == Decimal("101")  # noqa: SLF001
        )
        await snapshots.responses.put(
            {apple: QuoteUpdate(Decimal("100"), "USD", None, Decimal("95"), "2026-09-12")}
        )
        await _wait_until(
            lambda: service._quotes[apple].baseline_date == "2026-09-12"  # noqa: SLF001
        )

        item = (await service.snapshot())["quotes"][0]
        assert item["price"] == "101"
        assert item["previous_close"] == "95"
        assert item["change"] == "6"
        await service.stop()

    run(scenario())


def test_transient_errors_reconnect_and_redeclare_latest_watchlist() -> None:
    async def scenario(error_code: str) -> None:
        first = FakeWebSocket()
        second = FakeWebSocket()
        connector = FakeConnector(first, second)
        service = LiveQuoteService(
            FakeTokenProvider(),
            ControlledSnapshotProvider(),
            connector,
            max_backoff=0.01,
        )
        apple = QuoteKey("us", "AAPL")
        await service.set_desired([apple])
        await service.start()
        await next_declaration(first)
        await first.incoming.put(json.dumps({"type": "error", "error": {"code": error_code}}))

        second_id, second_declaration = await next_declaration(second)
        assert second_declaration[1] == {"type": "trade:us", "codes": ["AAPL"]}
        await second.incoming.put(ack(second_id, [apple.topic], []))
        await _wait_until(lambda: service._connection_status == "connected")  # noqa: SLF001
        assert len(connector.tokens) == 2
        await service.stop()

    for error_code in ("rate-limit-exceeded", "internal-error"):
        run(scenario(error_code))


def test_deleted_key_ignores_late_tick_and_stale_snapshot() -> None:
    async def scenario() -> None:
        snapshots = ControlledSnapshotProvider()
        socket = FakeWebSocket()
        service = LiveQuoteService(
            FakeTokenProvider(),
            snapshots,
            FakeConnector(socket),
            subscription_debounce=0,
        )
        apple = QuoteKey("us", "AAPL")
        await service.set_desired([apple])
        await service.start()
        await next_declaration(socket)
        await service.set_desired([])
        await socket.incoming.put(trade(apple, "999"))
        await snapshots.responses.put(
            {apple: QuoteUpdate(Decimal("888"), "USD", "2026-09-12T09:30:00+09:00")}
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert (await service.snapshot())["quotes"] == []
        assert await asyncio.wait_for(socket.sent.get(), timeout=1) == "[]"
        await service.stop()

    run(scenario())


def test_ping_requires_pong_and_reconnects_with_bounded_backoff() -> None:
    async def scenario() -> None:
        first = FakeWebSocket()
        second = FakeWebSocket()
        connector = FakeConnector(first, second)
        snapshots = ControlledSnapshotProvider()
        service = LiveQuoteService(
            FakeTokenProvider(),
            snapshots,
            connector,
            ping_interval=0.01,
            pong_timeout=0.01,
            max_backoff=0.01,
            jitter=lambda: 1.0,
        )
        apple = QuoteKey("us", "AAPL")
        await service.set_desired([apple])
        await service.start()
        first_id, _ = await next_declaration(first)
        await first.incoming.put(ack(first_id, [apple.topic], []))
        await first.incoming.put(trade(apple, "101"))
        await _wait_until(
            lambda: service._quotes[apple].price == Decimal("101")  # noqa: SLF001
        )
        assert await asyncio.wait_for(first.sent.get(), timeout=1) == "PING"

        await asyncio.wait_for(_wait_until(lambda: len(connector.tokens) == 2), timeout=1)
        assert connector.tokens == ["secret-token", "secret-token"]
        assert (await service.health())["reconnect_attempt"] == 1
        second_id, _ = await next_declaration(second)
        await second.incoming.put(ack(second_id, [apple.topic], []))
        await _wait_until(lambda: service._connection_status == "connected")  # noqa: SLF001
        assert (await service.snapshot())["quotes"][0]["status"] == "delayed"
        await service.stop()

    run(scenario())


def test_pong_keeps_connection_alive() -> None:
    async def scenario() -> None:
        socket = FakeWebSocket()
        connector = FakeConnector(socket)
        service = LiveQuoteService(
            FakeTokenProvider(),
            ControlledSnapshotProvider(),
            connector,
            ping_interval=0.01,
            pong_timeout=0.05,
        )
        await service.set_desired([QuoteKey("us", "AAPL")])
        await service.start()
        await next_declaration(socket)
        assert await asyncio.wait_for(socket.sent.get(), timeout=1) == "PING"
        await socket.incoming.put('{"type":"pong"}')
        await asyncio.sleep(0.02)

        assert len(connector.tokens) == 1
        await service.stop()

    run(scenario())


def test_decimal_change_is_serialized_without_float_and_shutdown_is_clean() -> None:
    async def scenario() -> None:
        snapshots = ControlledSnapshotProvider()
        socket = FakeWebSocket()
        service = LiveQuoteService(
            FakeTokenProvider(),
            snapshots,
            FakeConnector(socket),
            now=lambda: datetime(2026, 9, 12, 0, 30, tzinfo=UTC),
        )
        apple = QuoteKey("us", "AAPL")
        await service.set_desired([apple])
        await service.start()
        request_id, _ = await next_declaration(socket)
        await socket.incoming.put(ack(request_id, [apple.topic], []))
        await snapshots.responses.put(
            {
                apple: QuoteUpdate(
                    Decimal("101.25"),
                    "USD",
                    "2026-09-12T09:30:00+09:00",
                    Decimal("100"),
                )
            }
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        item = (await service.snapshot())["quotes"][0]
        assert item["price"] == "101.25"
        assert item["change"] == "1.25"
        assert item["change_percent"] == "1.2500"
        assert item["direction"] == "up"
        assert item["received_at"] == "2026-09-12T00:30:00+00:00"
        assert isinstance(item["price"], str)

        await service.stop()
        assert (await service.health())["status"] == "stopped"

    run(scenario())


async def _wait_until(predicate: object) -> None:
    for _ in range(100):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met")
