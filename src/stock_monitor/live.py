from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

PING_INTERVAL_SECONDS = 60.0
PONG_TIMEOUT_SECONDS = 15.0
SUBSCRIPTION_DEBOUNCE_SECONDS = 0.25
MAX_BACKOFF_SECONDS = 60.0
SNAPSHOT_INTERVAL_SECONDS = 900.0
MAX_WATCHLIST_ITEMS = 20


@dataclass(frozen=True, order=True)
class QuoteKey:
    market: str
    symbol: str

    def __post_init__(self) -> None:
        market = self.market.lower()
        symbol = self.symbol.upper()
        if market not in {"kr", "us"} or not symbol:
            raise ValueError("invalid quote key")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "symbol", symbol)

    @property
    def topic(self) -> str:
        return f"trade:{self.market}:{self.symbol}"


@dataclass(frozen=True)
class AccessToken:
    value: str
    generation: int

    def __repr__(self) -> str:
        return f"AccessToken(value=<redacted>, generation={self.generation})"


@dataclass(frozen=True)
class QuoteUpdate:
    price: Decimal
    currency: str
    provider_at: str | None
    previous_close: Decimal | None = None
    baseline_date: str | None = None


@dataclass(frozen=True)
class Quote:
    key: QuoteKey
    price: Decimal | None = None
    currency: str | None = None
    provider_at: str | None = None
    previous_close: Decimal | None = None
    baseline_date: str | None = None
    received_at: str | None = None
    generation: int | None = None
    status: str = "pending"
    error: str | None = None


class TokenProvider(Protocol):
    async def get_token(self) -> AccessToken: ...

    async def invalidate(self, generation: int) -> None: ...


class SnapshotProvider(Protocol):
    async def fetch(
        self, token: AccessToken, keys: tuple[QuoteKey, ...]
    ) -> Mapping[QuoteKey, QuoteUpdate]: ...


class WebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class WebSocketConnector(Protocol):
    async def connect(self, access_token: str) -> WebSocket: ...


class LiveDependencyError(Exception):
    """A dependency failure expressed only with a safe, stable code."""

    def __init__(self, code: str, *, refresh_token: bool = False):
        super().__init__(code)
        self.code = code
        self.refresh_token = refresh_token


@dataclass(frozen=True)
class Subscription:
    queue: asyncio.Queue[dict[str, object]]


class _ConnectionClosed(Exception):
    def __init__(self, code: str = "connection_lost") -> None:
        super().__init__(code)
        self.code = code


class _PongTimeout(Exception):
    pass


class LiveQuoteService:
    """Owns one upstream connection and fans its latest state out to subscribers."""

    def __init__(
        self,
        token_provider: TokenProvider,
        snapshot_provider: SnapshotProvider,
        connector: WebSocketConnector,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        ping_interval: float = PING_INTERVAL_SECONDS,
        pong_timeout: float = PONG_TIMEOUT_SECONDS,
        subscription_debounce: float = SUBSCRIPTION_DEBOUNCE_SECONDS,
        snapshot_interval: float = SNAPSHOT_INTERVAL_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self._token_provider = token_provider
        self._snapshot_provider = snapshot_provider
        self._connector = connector
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep
        self._jitter = jitter
        self._ping_interval = ping_interval
        self._pong_timeout = pong_timeout
        self._subscription_debounce = subscription_debounce
        self._snapshot_interval = snapshot_interval
        self._max_backoff = max_backoff

        self._lock = asyncio.Lock()
        self._desired: tuple[QuoteKey, ...] = ()
        self._desired_version = 0
        self._quotes: dict[QuoteKey, Quote] = {}
        self._quote_versions: dict[QuoteKey, int] = {}
        self._subscribed: set[QuoteKey] = set()
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._revision = 0
        self._connection_generation = 0
        self._connection_status = "stopped"
        self._last_error: str | None = None
        self._last_message_at: float | None = None
        self._reconnect_attempt = 0

        self._desired_changed = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._connection: WebSocket | None = None

    async def start(self) -> None:
        async with self._lock:
            if self._task is not None:
                return
            self._stopping = False
            self._desired_changed.clear()
            self._connection_status = "idle" if not self._desired else "connecting"
            self._task = asyncio.create_task(self._supervise(), name="stock-monitor-live")
            self._publish_locked()

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            self._task = None
            self._stopping = True
            connection = self._connection
            self._desired_changed.set()
        if connection is not None:
            with suppress(Exception):
                await asyncio.wait_for(connection.close(), timeout=5)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with self._lock:
            self._connection = None
            self._subscribed.clear()
            self._connection_status = "stopped"
            self._mark_quotes_locked("delayed")
            self._publish_locked()

    async def set_desired(self, keys: Iterable[QuoteKey]) -> None:
        normalized = tuple(dict.fromkeys(keys))
        if len(normalized) > MAX_WATCHLIST_ITEMS:
            raise ValueError("too many quote keys")
        async with self._lock:
            if normalized == self._desired:
                return
            previous = set(self._desired)
            current = set(normalized)
            self._desired = normalized
            self._desired_version += 1
            for key in previous - current:
                self._quotes.pop(key, None)
                self._quote_versions.pop(key, None)
                self._subscribed.discard(key)
            for key in current - previous:
                self._quotes[key] = Quote(key=key)
                self._quote_versions[key] = 0
            self._desired_changed.set()
            self._publish_locked()

    async def subscribe(self) -> Subscription:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
            queue.put_nowait(self._snapshot_locked())
        return Subscription(queue)

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._lock:
            self._subscribers.discard(subscription.queue)

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            return self._snapshot_locked()

    async def health(self) -> dict[str, object]:
        async with self._lock:
            age = None
            if self._last_message_at is not None:
                age = max(0.0, self._monotonic() - self._last_message_at)
            return {
                "status": self._connection_status,
                "desired": len(self._desired),
                "subscribed": len(self._subscribed),
                "subscribers": len(self._subscribers),
                "reconnect_attempt": self._reconnect_attempt,
                "last_message_age_seconds": round(age, 3) if age is not None else None,
                "error": self._last_error,
            }

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stopping:
            desired = await self._desired_snapshot()
            if not desired:
                await self._set_connection_state("idle")
                await self._wait_for_change()
                attempt = 0
                continue

            token: AccessToken | None = None
            try:
                await self._set_connection_state("connecting" if attempt == 0 else "delayed")
                token = await self._token_provider.get_token()
                connection = await self._connector.connect(token.value)
                async with self._lock:
                    stopping = self._stopping
                    if not stopping:
                        self._connection = connection
                        self._connection_generation += 1
                        generation = self._connection_generation
                if stopping:
                    with suppress(Exception):
                        await asyncio.wait_for(connection.close(), timeout=5)
                    return
                outcome = await self._run_connection(connection, token, generation)
                if outcome == "idle":
                    attempt = 0
                    continue
                raise _ConnectionClosed
            except asyncio.CancelledError:
                raise
            except LiveDependencyError as error:
                if error.refresh_token and token is not None:
                    await self._token_provider.invalidate(token.generation)
                code = error.code
            except _PongTimeout:
                code = "pong_timeout"
            except _ConnectionClosed as error:
                code = error.code
            except Exception:
                code = "connection_lost"
            finally:
                async with self._lock:
                    self._connection = None
                    self._subscribed.clear()

            if self._stopping:
                break
            async with self._lock:
                was_connected = self._connection_status == "connected"
            attempt = 1 if was_connected else attempt + 1
            async with self._lock:
                self._reconnect_attempt = attempt
                if code in {"auth_error", "ip_forbidden"}:
                    self._connection_status = code
                else:
                    self._connection_status = "disconnected" if attempt >= 6 else "delayed"
                self._last_error = code
                self._mark_quotes_locked("delayed")
                self._publish_locked()
            base = min(2 ** (attempt - 1), self._max_backoff)
            delay = min(self._max_backoff, base + (base * 0.25 * self._bounded_jitter()))
            await self._sleep_or_change(delay)

    async def _run_connection(
        self, connection: WebSocket, token: AccessToken, generation: int
    ) -> str:
        pending: dict[str, tuple[int, dict[str, QuoteKey]]] = {}
        request_number = 0
        pong_received = asyncio.Event()
        snapshot_task: asyncio.Task[None] | None = None
        periodic_snapshot_task: asyncio.Task[None] | None = None

        async def declare() -> None:
            nonlocal request_number, snapshot_task
            desired, version = await self._desired_and_version()
            if not desired:
                await connection.send("[]")
                return
            request_number += 1
            request_id = f"{generation}-{request_number}"
            topic_map = {key.topic: key for key in desired}
            pending[request_id] = (version, topic_map)
            await connection.send(_declaration(desired, request_id))
            if snapshot_task is not None:
                snapshot_task.cancel()
            markers = await self._version_markers(desired)
            snapshot_task = asyncio.create_task(
                self._refresh_snapshot(token, desired, version, generation, markers)
            )

        async def refresh_periodically() -> None:
            while True:
                await self._sleep(self._snapshot_interval)
                desired, version = await self._desired_and_version()
                if not desired:
                    continue
                markers = await self._version_markers(desired)
                await self._refresh_snapshot(token, desired, version, generation, markers)

        await declare()
        periodic_snapshot_task = asyncio.create_task(refresh_periodically())
        receive_task = asyncio.create_task(connection.recv())
        change_task = asyncio.create_task(self._desired_changed.wait())
        ping_task = asyncio.create_task(self._sleep(self._ping_interval))
        pong_timeout_task: asyncio.Task[None] | None = None
        try:
            while True:
                waiters: set[asyncio.Task[object]] = {
                    receive_task,  # type: ignore[arg-type]
                    change_task,  # type: ignore[arg-type]
                    ping_task,  # type: ignore[arg-type]
                }
                if pong_timeout_task is not None:
                    waiters.add(pong_timeout_task)  # type: ignore[arg-type]
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

                if receive_task in done:
                    frame = receive_task.result()
                    await self._handle_frame(frame, generation, pending, pong_received)
                    receive_task = asyncio.create_task(connection.recv())

                if pong_timeout_task is not None and pong_timeout_task in done:
                    if not pong_received.is_set():
                        raise _PongTimeout
                    pong_timeout_task = None

                if ping_task in done:
                    pong_received.clear()
                    await connection.send("PING")
                    if pong_timeout_task is not None:
                        pong_timeout_task.cancel()
                    pong_timeout_task = asyncio.create_task(self._sleep(self._pong_timeout))
                    ping_task = asyncio.create_task(self._sleep(self._ping_interval))

                if change_task in done:
                    self._desired_changed.clear()
                    await self._sleep(self._subscription_debounce)
                    desired = await self._desired_snapshot()
                    if not desired:
                        await connection.send("[]")
                        return "idle"
                    await declare()
                    change_task = asyncio.create_task(self._desired_changed.wait())
        finally:
            tasks = [
                receive_task,
                change_task,
                ping_task,
                pong_timeout_task,
                snapshot_task,
                periodic_snapshot_task,
            ]
            for task in tasks:
                if task is not None:
                    task.cancel()
            cleanup = asyncio.gather(
                *(task for task in tasks if task is not None), return_exceptions=True
            )
            with suppress(Exception):
                await asyncio.wait_for(cleanup, timeout=5)
            with suppress(Exception):
                await asyncio.wait_for(connection.close(), timeout=5)

    async def _handle_frame(
        self,
        raw: str | bytes,
        generation: int,
        pending: dict[str, tuple[int, dict[str, QuoteKey]]],
        pong_received: asyncio.Event,
    ) -> None:
        if isinstance(raw, bytes):
            return
        try:
            frame = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        if frame_type == "pong":
            pong_received.set()
            return
        if frame_type == "subscriptions":
            await self._handle_ack(frame, generation, pending)
            return
        if frame_type == "message":
            await self._handle_message(frame, generation)
            return
        if frame_type == "error":
            error = frame.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code in {"server-shutdown", "rate-limit-exceeded", "internal-error"}:
                safe_code = {
                    "server-shutdown": "server_shutdown",
                    "rate-limit-exceeded": "rate_limited",
                    "internal-error": "server_error",
                }[code]
                raise _ConnectionClosed(safe_code)
            async with self._lock:
                self._last_error = code if isinstance(code, str) else "websocket_error"
                self._publish_locked()

    async def _handle_ack(
        self,
        frame: dict[str, object],
        generation: int,
        pending: dict[str, tuple[int, dict[str, QuoteKey]]],
    ) -> None:
        request_id = frame.get("id")
        subscribed = frame.get("subscribed")
        rejected = frame.get("rejected")
        if (
            not isinstance(request_id, str)
            or not isinstance(subscribed, list)
            or not isinstance(rejected, list)
        ):
            return
        request = pending.pop(request_id, None)
        if request is None:
            return
        desired_version, topics = request
        accepted = {
            topics[topic] for topic in subscribed if isinstance(topic, str) and topic in topics
        }
        failures: dict[QuoteKey, str] = {}
        for item in rejected:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            code = item.get("code")
            if isinstance(target, str) and target in topics and isinstance(code, str):
                failures[topics[target]] = code
        async with self._lock:
            if (
                generation != self._connection_generation
                or desired_version != self._desired_version
            ):
                return
            desired = set(self._desired)
            self._subscribed = accepted & desired
            self._connection_status = "connected"
            self._last_error = None
            self._reconnect_attempt = 0
            for key, code in failures.items():
                if key in desired:
                    self._quotes[key] = replace(self._quotes[key], status="unavailable", error=code)
            for key in self._subscribed:
                quote = self._quotes[key]
                if quote.price is not None and quote.generation == generation:
                    self._quotes[key] = replace(quote, status="live", error=None)
            self._publish_locked()

    async def _handle_message(self, frame: dict[str, object], generation: int) -> None:
        topic = frame.get("topic")
        data = frame.get("data")
        if not isinstance(topic, str) or not isinstance(data, dict):
            return
        parts = topic.split(":", 2)
        if len(parts) != 3 or parts[0] != "trade" or parts[1] not in {"kr", "us"}:
            return
        try:
            key = QuoteKey(parts[1], parts[2])
            price = _decimal(data.get("price"))
        except ValueError:
            return
        currency = data.get("currency")
        provider_at = data.get("timestamp")
        if not isinstance(currency, str) or not currency or not isinstance(provider_at, str):
            return
        async with self._lock:
            if generation != self._connection_generation or key not in self._desired:
                return
            current = self._quotes[key]
            self._quotes[key] = replace(
                current,
                price=price,
                currency=currency,
                provider_at=provider_at,
                received_at=self._now().isoformat(),
                generation=generation,
                status="live",
                error=None,
            )
            self._quote_versions[key] += 1
            self._last_message_at = self._monotonic()
            self._publish_locked()

    async def _refresh_snapshot(
        self,
        token: AccessToken,
        keys: tuple[QuoteKey, ...],
        desired_version: int,
        generation: int,
        markers: Mapping[QuoteKey, int],
    ) -> None:
        try:
            updates = await self._snapshot_provider.fetch(token, keys)
        except asyncio.CancelledError:
            raise
        except LiveDependencyError as error:
            if error.refresh_token:
                await self._token_provider.invalidate(token.generation)
            async with self._lock:
                if generation == self._connection_generation:
                    self._last_error = error.code
                    self._publish_locked()
            return
        except Exception:
            async with self._lock:
                if generation == self._connection_generation:
                    self._last_error = "snapshot_failed"
                    self._publish_locked()
            return

        async with self._lock:
            if (
                generation != self._connection_generation
                or desired_version != self._desired_version
            ):
                return
            desired = set(self._desired)
            for key, update in updates.items():
                if key not in desired:
                    continue
                if not _valid_update(update):
                    continue
                current = self._quotes[key]
                previous_close, baseline_date = _merged_baseline(current, update)
                if self._quote_versions.get(key) != markers.get(key):
                    if (
                        previous_close != current.previous_close
                        or baseline_date != current.baseline_date
                    ):
                        self._quotes[key] = replace(
                            current,
                            previous_close=previous_close,
                            baseline_date=baseline_date,
                        )
                    continue
                self._quotes[key] = replace(
                    current,
                    price=update.price,
                    currency=update.currency,
                    provider_at=update.provider_at,
                    previous_close=previous_close,
                    baseline_date=baseline_date,
                    received_at=self._now().isoformat(),
                    generation=generation,
                    status=(
                        "unavailable"
                        if current.status == "unavailable"
                        else "live"
                        if key in self._subscribed
                        else "pending"
                    ),
                    error=None,
                )
                self._quote_versions[key] += 1
            self._publish_locked()

    async def _desired_snapshot(self) -> tuple[QuoteKey, ...]:
        async with self._lock:
            return self._desired

    async def _desired_and_version(self) -> tuple[tuple[QuoteKey, ...], int]:
        async with self._lock:
            return self._desired, self._desired_version

    async def _version_markers(self, keys: tuple[QuoteKey, ...]) -> dict[QuoteKey, int]:
        async with self._lock:
            return {key: self._quote_versions[key] for key in keys if key in self._quote_versions}

    async def _set_connection_state(self, status: str) -> None:
        async with self._lock:
            if self._connection_status != status:
                self._connection_status = status
                self._publish_locked()

    async def _wait_for_change(self) -> None:
        self._desired_changed.clear()
        if await self._desired_snapshot():
            return
        await self._desired_changed.wait()

    async def _sleep_or_change(self, delay: float) -> None:
        sleep_task = asyncio.create_task(self._sleep(delay))
        change_task = asyncio.create_task(self._desired_changed.wait())
        done, pending = await asyncio.wait(
            {sleep_task, change_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    def _mark_quotes_locked(self, status: str) -> None:
        for key in self._desired:
            quote = self._quotes[key]
            if quote.status == "unavailable":
                continue
            next_status = status if quote.price is not None else "disconnected"
            self._quotes[key] = replace(quote, status=next_status)

    def _publish_locked(self) -> None:
        self._revision += 1
        snapshot = self._snapshot_locked()
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(snapshot)

    def _snapshot_locked(self) -> dict[str, object]:
        return {
            "revision": self._revision,
            "connection": {
                "status": self._connection_status,
                "error": self._last_error,
            },
            "quotes": [_quote_dict(self._quotes[key]) for key in self._desired],
        }

    def _bounded_jitter(self) -> float:
        return min(max(self._jitter(), 0.0), 1.0)


def _declaration(keys: tuple[QuoteKey, ...], request_id: str) -> str:
    kr = [key.symbol for key in keys if key.market == "kr"]
    us = [key.symbol for key in keys if key.market == "us"]
    declaration: list[dict[str, object]] = [{"id": request_id}]
    if kr:
        declaration.append({"type": "trade:kr", "codes": kr})
    if us:
        declaration.append({"type": "trade:us", "codes": us})
    return json.dumps(declaration, separators=(",", ":"))


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("invalid decimal")
    try:
        decimal = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid decimal") from error
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("invalid decimal")
    return decimal


def _valid_update(update: object) -> bool:
    return (
        isinstance(update, QuoteUpdate)
        and update.price.is_finite()
        and update.price >= 0
        and bool(update.currency)
        and (
            update.previous_close is None
            or (update.previous_close.is_finite() and update.previous_close >= 0)
        )
    )


def _merged_baseline(current: Quote, update: QuoteUpdate) -> tuple[Decimal | None, str | None]:
    if update.previous_close is None:
        return current.previous_close, current.baseline_date
    if update.baseline_date is None:
        if current.previous_close is None:
            return update.previous_close, None
        return current.previous_close, current.baseline_date
    if current.baseline_date is None or update.baseline_date >= current.baseline_date:
        return update.previous_close, update.baseline_date
    return current.previous_close, current.baseline_date


def _quote_dict(quote: Quote) -> dict[str, object]:
    change = None
    change_percent = None
    if quote.price is not None and quote.previous_close is not None:
        change = quote.price - quote.previous_close
        if quote.previous_close != 0:
            change_percent = change / quote.previous_close * Decimal(100)
    direction = None
    if change is not None:
        direction = "up" if change > 0 else "down" if change < 0 else "flat"
    return {
        "market": quote.key.market.upper(),
        "symbol": quote.key.symbol,
        "price": str(quote.price) if quote.price is not None else None,
        "currency": quote.currency,
        "provider_at": quote.provider_at,
        "received_at": quote.received_at,
        "previous_close": (str(quote.previous_close) if quote.previous_close is not None else None),
        "baseline_date": quote.baseline_date,
        "change": str(change) if change is not None else None,
        "change_percent": str(change_percent) if change_percent is not None else None,
        "direction": direction,
        "status": quote.status,
        "error": quote.error,
    }
