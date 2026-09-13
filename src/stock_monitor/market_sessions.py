from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from .market_data import Country, MarketCalendar, MarketDataError, MarketDay, MarketSession

CALENDAR_REFRESH_INTERVAL_SECONDS = 900.0
INITIAL_RETRY_SECONDS = 30.0
LOGGER = logging.getLogger(__name__)

SourceStatus = Literal["loading", "ready", "stale", "error"]
MarketPhase = Literal["preopen", "open", "between", "closed", "holiday", "unknown"]
SessionName = Literal["day", "pre", "regular", "after"]
EventKind = Literal["start", "end"]


@dataclass(frozen=True)
class NextMarketEvent:
    kind: EventKind
    session: SessionName
    at: datetime

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "session": self.session, "at": self.at.isoformat()}


@dataclass(frozen=True)
class MarketStatus:
    country: Country
    status: SourceStatus
    phase: MarketPhase
    session: SessionName | None
    next_event: NextMarketEvent | None
    refreshed_at: datetime | None
    error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "country": self.country,
            "status": self.status,
            "phase": self.phase,
            "session": self.session,
            "next_event": self.next_event.as_dict() if self.next_event else None,
            "refreshed_at": self.refreshed_at.isoformat() if self.refreshed_at else None,
            "error": self.error,
        }


class CalendarClient(Protocol):
    async def get_market_calendar(self, country: Country) -> MarketCalendar: ...


MarketPublisher = Callable[[tuple[MarketStatus, ...]], Awaitable[None]]


@dataclass(frozen=True)
class _Window:
    session: SessionName
    start: datetime
    end: datetime
    day: Literal["previous", "today", "next"]


def evaluate_calendar(
    calendar: MarketCalendar,
    now: datetime,
    *,
    status: SourceStatus = "ready",
    refreshed_at: datetime | None = None,
    error: str | None = None,
) -> MarketStatus:
    _require_aware(now)
    windows = _windows(calendar)
    active_component = _active_component(windows, now)
    if active_component is not None:
        component, active = active_component
        current = max(active, key=lambda item: (item.start, _session_rank(item.session)))
        return MarketStatus(
            calendar.country,
            status,
            "open",
            current.session,
            NextMarketEvent("end", current.session, component[-1].end),
            refreshed_at,
            error,
        )

    future = [window for window in windows if window.start > now]
    next_window = min(future, key=lambda item: item.start) if future else None
    next_event = (
        NextMarketEvent("start", next_window.session, next_window.start) if next_window else None
    )
    today = [window for window in windows if window.day == "today"]
    if not today:
        phase: MarketPhase = "holiday"
    elif next_window is not None and next_window.day == "today":
        phase = "between" if any(window.end <= now for window in today) else "preopen"
    elif all(window.end <= now for window in today):
        phase = "closed"
    elif all(now < window.start for window in today):
        phase = "preopen"
    else:
        phase = "between"
    if status == "stale" and windows and now >= max(window.end for window in windows):
        phase = "unknown"
        next_event = None
    return MarketStatus(
        calendar.country,
        status,
        phase,
        None,
        next_event,
        refreshed_at,
        error,
    )


class MarketSessionMonitor:
    def __init__(
        self,
        client: CalendarClient,
        publish: MarketPublisher,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        refresh_interval: float = CALENDAR_REFRESH_INTERVAL_SECONDS,
        max_backoff: float = CALENDAR_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        if refresh_interval <= 0 or max_backoff <= 0:
            raise ValueError("intervals_must_be_positive")
        self._client = client
        self._publish = publish
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._refresh_interval = refresh_interval
        self._max_backoff = max_backoff
        self._lock = asyncio.Lock()
        self._calendars: dict[Country, MarketCalendar] = {}
        self._refreshed: dict[Country, datetime] = {}
        self._source_status: dict[Country, SourceStatus] = {"KR": "loading", "US": "loading"}
        self._errors: dict[Country, str | None] = {"KR": None, "US": None}
        self._deadlines: dict[Country, float] = {"KR": 0.0, "US": 0.0}
        self._failures: dict[Country, int] = {"KR": 0, "US": 0}
        self._last_published: tuple[MarketStatus, ...] | None = None
        self._task: asyncio.Task[None] | None = None
        self._refresh_tasks: dict[Country, asyncio.Task[None]] = {}
        self._changed = asyncio.Event()

    async def start(self) -> None:
        async with self._lock:
            if self._task is not None:
                return
            loading = tuple(self._unknown(country, "loading") for country in ("KR", "US"))
            self._last_published = loading
            await self._publish(loading)
            self._task = asyncio.create_task(self._run(), name="stock-monitor-market-sessions")

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            self._task = None
            refresh_tasks = tuple(self._refresh_tasks.values())
            self._refresh_tasks.clear()
        tasks = tuple(item for item in (task, *refresh_tasks) if item is not None)
        for item in tasks:
            item.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def get(self, country: Country) -> MarketCalendar | None:
        async with self._lock:
            return self._calendars.get(country)

    async def _run(self) -> None:
        while True:
            self._changed.clear()
            now = self._now()
            _require_aware(now)
            current_tick = self._monotonic()
            await self._publish_current(now)
            await self._start_due_refreshes(current_tick)
            refreshed_now = self._now()
            await self._publish_current(refreshed_now)
            if self._changed.is_set():
                continue
            delay = await self._next_delay(refreshed_now, self._monotonic())
            await self._sleep_or_change(delay)

    async def _start_due_refreshes(self, current_tick: float) -> None:
        async with self._lock:
            for country in ("KR", "US"):
                if current_tick < self._deadlines[country] or country in self._refresh_tasks:
                    continue
                self._deadlines[country] = math.inf
                self._refresh_tasks[country] = asyncio.create_task(
                    self._refresh_one(country),
                    name=f"stock-monitor-market-calendar-{country.lower()}",
                )

    async def _refresh_one(self, country: Country) -> None:
        try:
            await self._refresh(country)
        finally:
            async with self._lock:
                if self._refresh_tasks.get(country) is asyncio.current_task():
                    self._refresh_tasks.pop(country, None)
            self._changed.set()

    async def _refresh(self, country: Country) -> None:
        try:
            calendar = await self._client.get_market_calendar(country)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            finished_tick = self._monotonic()
            error_code = _calendar_error_code(error)
            LOGGER.warning(
                "market_calendar_refresh_failed country=%s code=%s",
                country,
                error_code,
            )
            async with self._lock:
                self._failures[country] += 1
                cached = country in self._calendars
                self._source_status[country] = "stale" if cached else "error"
                self._errors[country] = error_code
                backoff = min(
                    INITIAL_RETRY_SECONDS * 2 ** (self._failures[country] - 1),
                    self._max_backoff,
                )
                if isinstance(error, MarketDataError):
                    backoff = max(backoff, _retry_after(error.retry_after_seconds))
                self._deadlines[country] = finished_tick + backoff
            return
        if calendar.country != country:
            finished_tick = self._monotonic()
            async with self._lock:
                self._failures[country] += 1
                self._source_status[country] = "stale" if country in self._calendars else "error"
                self._errors[country] = "calendar_unavailable"
                self._deadlines[country] = finished_tick + min(
                    INITIAL_RETRY_SECONDS * 2 ** (self._failures[country] - 1),
                    self._max_backoff,
                )
            return
        finished_at = self._now()
        finished_tick = self._monotonic()
        async with self._lock:
            self._calendars[country] = calendar
            self._refreshed[country] = finished_at
            self._source_status[country] = "ready"
            self._errors[country] = None
            self._failures[country] = 0
            self._deadlines[country] = finished_tick + self._refresh_interval

    async def _publish_current(self, now: datetime) -> None:
        async with self._lock:
            statuses = tuple(self._evaluate(country, now) for country in ("KR", "US"))
            if statuses == self._last_published:
                return
            self._last_published = statuses
        await self._publish(statuses)

    async def _next_delay(self, now: datetime, current_tick: float) -> float:
        async with self._lock:
            refresh_delay = min(self._deadlines.values()) - current_tick
            boundaries = [
                boundary
                for calendar in self._calendars.values()
                for boundary in _boundaries(calendar)
                if boundary > now
            ]
        boundary_delay = (
            min((item - now).total_seconds() for item in boundaries) if boundaries else None
        )
        delay = refresh_delay if boundary_delay is None else min(refresh_delay, boundary_delay)
        if not math.isfinite(delay):
            delay = self._refresh_interval
        return max(delay, 0.001)

    async def _sleep_or_change(self, delay: float) -> None:
        sleep_task = asyncio.create_task(
            self._sleep(delay), name="stock-monitor-market-session-sleep"
        )
        change_task = asyncio.create_task(
            self._changed.wait(), name="stock-monitor-market-session-change"
        )
        tasks = (sleep_task, change_task)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _evaluate(self, country: Country, now: datetime) -> MarketStatus:
        calendar = self._calendars.get(country)
        status = self._source_status[country]
        if calendar is None:
            return self._unknown(country, status)
        return evaluate_calendar(
            calendar,
            now,
            status=status,
            refreshed_at=self._refreshed[country],
            error=self._errors[country],
        )

    def _unknown(self, country: Country, status: SourceStatus) -> MarketStatus:
        return MarketStatus(
            country=country,
            status=status,
            phase="unknown",
            session=None,
            next_event=None,
            refreshed_at=self._refreshed.get(country),
            error=self._errors.get(country),
        )


def _windows(calendar: MarketCalendar) -> list[_Window]:
    windows: list[_Window] = []
    for day_name, day in (
        ("previous", calendar.previous_business_day),
        ("today", calendar.today),
        ("next", calendar.next_business_day),
    ):
        windows.extend(_day_windows(day, day_name))
    return sorted(windows, key=lambda item: (item.start, item.end, _session_rank(item.session)))


def _day_windows(day: MarketDay, day_name: Literal["previous", "today", "next"]) -> list[_Window]:
    result = []
    for name, session in (
        ("day", day.day_market),
        ("pre", day.pre_market),
        ("regular", day.regular_market),
        ("after", day.after_market),
    ):
        if session is not None:
            _validate_session(session)
            result.append(_Window(name, session.start_time, session.end_time, day_name))
    return result


def _active_component(
    windows: list[_Window], now: datetime
) -> tuple[list[_Window], list[_Window]] | None:
    components: list[list[_Window]] = []
    for window in windows:
        if not components or window.start > max(item.end for item in components[-1]):
            components.append([window])
        else:
            components[-1].append(window)
    for component in components:
        end = max(item.end for item in component)
        if component[0].start <= now < end:
            active = [item for item in component if item.start <= now < item.end]
            if active:
                ordered = sorted(component, key=lambda item: item.end)
                return ordered, active
    return None


def _boundaries(calendar: MarketCalendar) -> set[datetime]:
    return {value for window in _windows(calendar) for value in (window.start, window.end)}


def _validate_session(session: MarketSession) -> None:
    _require_aware(session.start_time)
    _require_aware(session.end_time)
    if session.start_time >= session.end_time:
        raise ValueError("market_session_end_must_follow_start")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime_must_be_timezone_aware")


def _session_rank(session: SessionName) -> int:
    return {"day": 0, "pre": 1, "regular": 2, "after": 3}[session]


def _calendar_error_code(error: Exception) -> str:
    if isinstance(error, MarketDataError):
        if error.status_code == 401:
            return "auth_error"
        if error.status_code == 403:
            return "ip_forbidden"
        if error.status_code == 429:
            return "rate_limited"
    return "calendar_unavailable"


def _retry_after(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        return max(float(value), 0.0)
    except ValueError:
        return 0.0
