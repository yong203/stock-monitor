from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_monitor.market_data import MarketCalendar, MarketDataError, MarketDay, MarketSession
from stock_monitor.market_sessions import (
    MarketSessionMonitor,
    MarketStatus,
    evaluate_calendar,
)


def at(hour: int, minute: int = 0, *, day: int = 14) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def session(start: datetime, end: datetime) -> MarketSession:
    return MarketSession(start, end)


def day(
    trading_day: int,
    *,
    day_market: MarketSession | None = None,
    pre: MarketSession | None = None,
    regular: MarketSession | None = None,
    after: MarketSession | None = None,
) -> MarketDay:
    return MarketDay(date(2026, 9, trading_day), day_market, pre, regular, after)


def calendar(
    country: str = "KR", *, today: MarketDay | None = None, previous: MarketDay | None = None
) -> MarketCalendar:
    return MarketCalendar(
        country,  # type: ignore[arg-type]
        today or day(14, regular=session(at(9), at(15, 30))),
        previous or day(13),
        day(15, regular=session(at(9, day=15), at(15, 30, day=15))),
    )


def test_active_sessions_use_half_open_boundaries_and_merge_touching_end() -> None:
    today = day(
        14,
        pre=session(at(8), at(9)),
        regular=session(at(9), at(15, 30)),
        after=session(at(15, 30), at(20)),
    )
    market = calendar(today=today)

    before = evaluate_calendar(market, at(7, 59))
    pre = evaluate_calendar(market, at(8, 30))
    exact_regular = evaluate_calendar(market, at(9))
    exact_close = evaluate_calendar(market, at(20))

    assert (before.phase, before.session) == ("preopen", None)
    assert before.next_event is not None and before.next_event.kind == "start"
    assert (pre.phase, pre.session) == ("open", "pre")
    assert pre.next_event is not None and pre.next_event.at == at(20)
    assert (exact_regular.phase, exact_regular.session) == ("open", "regular")
    assert exact_regular.next_event is not None and exact_regular.next_event.at == at(20)
    assert (exact_close.phase, exact_close.session) == ("closed", None)


def test_overlap_uses_latest_active_session_and_contiguous_final_end() -> None:
    today = day(
        14,
        pre=session(at(8), at(10)),
        regular=session(at(9), at(15)),
        after=session(at(14, 30), at(18)),
    )

    status = evaluate_calendar(calendar(today=today), at(9, 30))

    assert status.session == "regular"
    assert status.next_event is not None
    assert (status.next_event.kind, status.next_event.at) == ("end", at(18))


def test_gap_between_sessions_has_next_start() -> None:
    today = day(
        14,
        day_market=session(at(9), at(16, 50)),
        pre=session(at(17), at(22)),
    )

    status = evaluate_calendar(calendar("US", today=today), at(16, 55))

    assert (status.phase, status.session) == ("between", None)
    assert status.next_event is not None
    assert (status.next_event.kind, status.next_event.session, status.next_event.at) == (
        "start",
        "pre",
        at(17),
    )


def test_holiday_uses_next_business_day_start() -> None:
    market = calendar(today=day(14))

    status = evaluate_calendar(market, at(12))

    assert (status.phase, status.session) == ("holiday", None)
    assert status.next_event is not None
    assert status.next_event.at == at(9, day=15)


def test_previous_day_cross_midnight_session_is_considered() -> None:
    previous = day(
        13,
        regular=session(at(22, day=13), at(5, day=14)),
        after=session(at(5, day=14), at(7, day=14)),
    )
    today = day(14, day_market=session(at(9), at(16)))

    status = evaluate_calendar(calendar("US", today=today, previous=previous), at(6))

    assert (status.phase, status.session) == ("open", "after")
    assert status.next_event is not None and status.next_event.at == at(7)


def test_stale_calendar_past_all_known_sessions_becomes_unknown() -> None:
    market = calendar()

    status = evaluate_calendar(market, at(20, day=16), status="stale")

    assert status.phase == "unknown"
    assert status.next_event is None


def test_evaluator_rejects_naive_time_and_invalid_session() -> None:
    with pytest.raises(ValueError, match="timezone_aware"):
        evaluate_calendar(calendar(), datetime(2026, 9, 14, 9))
    invalid = calendar(today=day(14, regular=session(at(10), at(9))))
    with pytest.raises(ValueError, match="end_must_follow_start"):
        evaluate_calendar(invalid, at(9))


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.wall = value
        self.tick = 0.0
        self.sleeps: asyncio.Queue[tuple[float, asyncio.Future[None]]] = asyncio.Queue()

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.tick

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        await self.sleeps.put((seconds, future))
        await future

    async def advance_next(self) -> float:
        while True:
            seconds, future = await asyncio.wait_for(self.sleeps.get(), timeout=1)
            await asyncio.sleep(0)
            if not future.cancelled():
                break
        self.wall += timedelta(seconds=seconds)
        self.tick += seconds
        future.set_result(None)
        await asyncio.sleep(0)
        return seconds


class FakeClient:
    def __init__(self, calendars: dict[str, MarketCalendar]) -> None:
        self.calendars = calendars
        self.calls: list[str] = []
        self.fail: set[str] = set()

    async def get_market_calendar(self, country: str) -> MarketCalendar:
        self.calls.append(country)
        if country in self.fail:
            raise OSError("raw message must not escape")
        return self.calendars[country]


async def wait_for(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met")


def test_monitor_publishes_loading_refreshes_and_wakes_at_boundary() -> None:
    async def scenario() -> None:
        clock = FakeClock(at(8, 59))
        markets = {
            "KR": calendar(today=day(14, regular=session(at(9), at(15)))),
            "US": calendar("US", today=day(14, regular=session(at(10), at(16)))),
        }
        client = FakeClient(markets)
        published: list[tuple[MarketStatus, ...]] = []

        async def publish(statuses: tuple[MarketStatus, ...]) -> None:
            published.append(statuses)

        monitor = MarketSessionMonitor(
            client,
            publish,
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        await monitor.start()
        assert [item.status for item in published[0]] == ["loading", "loading"]
        await wait_for(lambda: client.calls == ["KR", "US"] and len(published) >= 2)
        assert [item.phase for item in published[-1]] == ["preopen", "preopen"]
        assert await monitor.get("KR") is markets["KR"]

        delay = await clock.advance_next()
        assert delay == 60
        await wait_for(lambda: published[-1][0].phase == "open")
        assert client.calls == ["KR", "US"]
        await monitor.stop()

    asyncio.run(scenario())


def test_monitor_retains_stale_calendar_retries_and_recovers() -> None:
    async def scenario() -> None:
        clock = FakeClock(at(1))
        markets = {"KR": calendar(), "US": calendar("US")}
        client = FakeClient(markets)
        published: list[tuple[MarketStatus, ...]] = []

        async def publish(statuses: tuple[MarketStatus, ...]) -> None:
            published.append(statuses)

        monitor = MarketSessionMonitor(
            client,
            publish,
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            refresh_interval=10,
            max_backoff=40,
        )
        await monitor.start()
        await wait_for(lambda: client.calls == ["KR", "US"] and len(published) >= 2)
        client.fail.add("KR")

        assert await clock.advance_next() == 10
        await wait_for(lambda: published[-1][0].status == "stale")
        stale = published[-1][0]
        assert stale.error == "calendar_unavailable"
        assert stale.phase != "unknown"
        assert await monitor.get("KR") is markets["KR"]

        assert await clock.advance_next() == 10
        await wait_for(
            lambda: client.calls.count("US") == 3 and "US" not in monitor._refresh_tasks  # noqa: SLF001
        )
        client.fail.remove("KR")
        while clock.tick < 40:
            await clock.advance_next()
        await wait_for(lambda: client.calls.count("KR") == 3)
        await wait_for(lambda: published[-1][0].status == "ready")
        await monitor.stop()

    asyncio.run(scenario())


def test_monitor_without_cached_calendar_reports_error_and_stops_cleanly() -> None:
    async def scenario() -> None:
        clock = FakeClock(at(1))
        client = FakeClient({"KR": calendar(), "US": calendar("US")})
        client.fail.add("KR")
        published: list[tuple[MarketStatus, ...]] = []

        async def publish(statuses: tuple[MarketStatus, ...]) -> None:
            published.append(statuses)

        monitor = MarketSessionMonitor(
            client,
            publish,
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        await monitor.start()
        await wait_for(lambda: len(published) >= 2)
        assert published[-1][0].as_dict() == {
            "country": "KR",
            "status": "error",
            "phase": "unknown",
            "session": None,
            "next_event": None,
            "refreshed_at": None,
            "error": "calendar_unavailable",
        }
        assert await monitor.get("KR") is None
        await wait_for(lambda: not clock.sleeps.empty())
        await monitor.stop()
        assert monitor._task is None  # noqa: SLF001
        assert not any(
            task.get_name().startswith("stock-monitor-market-session-")
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

    asyncio.run(scenario())


def test_monitor_preserves_safe_http_error_and_retry_after() -> None:
    async def scenario() -> None:
        clock = FakeClock(at(1))

        class RateLimitedClient(FakeClient):
            async def get_market_calendar(self, country: str) -> MarketCalendar:
                if country == "KR":
                    clock.tick += 10
                    clock.wall += timedelta(seconds=10)
                    raise MarketDataError(
                        "market_data_request_failed",
                        status_code=429,
                        retry_after_seconds="1200",
                    )
                return await super().get_market_calendar(country)

        client = RateLimitedClient({"US": calendar("US")})
        published: list[tuple[MarketStatus, ...]] = []

        async def publish(statuses: tuple[MarketStatus, ...]) -> None:
            published.append(statuses)

        monitor = MarketSessionMonitor(
            client,
            publish,
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            max_backoff=40,
        )
        await monitor.start()
        await wait_for(lambda: len(published) >= 2)

        assert published[-1][0].error == "rate_limited"
        await wait_for(lambda: "KR" not in monitor._refresh_tasks)  # noqa: SLF001
        assert monitor._deadlines["KR"] == 1210  # noqa: SLF001
        await monitor.stop()

    asyncio.run(scenario())


def test_monitor_publishes_boundary_before_due_refresh_completes() -> None:
    async def scenario() -> None:
        clock = FakeClock(at(8, 59))
        refresh_blocked = asyncio.Event()

        class BlockingRefreshClient(FakeClient):
            async def get_market_calendar(self, country: str) -> MarketCalendar:
                if len(self.calls) >= 2:
                    await refresh_blocked.wait()
                return await super().get_market_calendar(country)

        markets = {
            "KR": calendar(today=day(14, regular=session(at(9), at(15)))),
            "US": calendar("US", today=day(14, regular=session(at(10), at(16)))),
        }
        client = BlockingRefreshClient(markets)
        published: list[tuple[MarketStatus, ...]] = []

        async def publish(statuses: tuple[MarketStatus, ...]) -> None:
            published.append(statuses)

        monitor = MarketSessionMonitor(
            client,
            publish,
            now=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            refresh_interval=55,
        )
        await monitor.start()
        await wait_for(lambda: client.calls == ["KR", "US"] and len(published) >= 2)
        assert await clock.advance_next() == 55
        await wait_for(lambda: len(client.calls) == 2 and len(monitor._refresh_tasks) == 2)  # noqa: SLF001
        assert await clock.advance_next() == 5
        await wait_for(lambda: published[-1][0].phase == "open")
        assert len(client.calls) == 2
        await monitor.stop()

    asyncio.run(scenario())
