from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .database import MARKETS
from .diagnostics import (
    EXIT_AUTHENTICATION,
    EXIT_FORBIDDEN,
    EXIT_NETWORK,
    EXIT_PUBLIC_IP,
    EXIT_RATE_LIMIT,
    EXIT_REMOTE,
)
from .settings import Credentials
from .toss import TossClient, TossResponse, parse_access_token
from .watchlist import Instrument, replace_instrument_catalog

SUPPORTED_SECURITY_TYPES = {"STOCK", "FOREIGN_STOCK", "ETF", "FOREIGN_ETF"}
US_MARKETS = {"NYSE", "NASDAQ", "AMEX", "US_ETC"}


class InstrumentSyncError(Exception):
    def __init__(
        self,
        code: str,
        exit_code: int,
        *,
        stage: str,
        market: str | None = None,
        http_status: int | None = None,
        provider_code: str | None = None,
        request_id: str | None = None,
        retry_after_seconds: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.stage = stage
        self.market = market
        self.http_status = http_status
        self.provider_code = provider_code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds

    def safe_dict(self) -> dict[str, str | int | bool]:
        result: dict[str, str | int | bool] = {
            "ok": False,
            "stage": self.stage,
            "code": self.code,
            "exit_code": self.exit_code,
        }
        if self.market:
            result["market"] = self.market
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.provider_code:
            result["provider_code"] = self.provider_code
        if self.request_id:
            result["request_id"] = self.request_id
        if self.retry_after_seconds:
            result["retry_after_seconds"] = self.retry_after_seconds
        return result


@dataclass(frozen=True)
class InstrumentSyncResult:
    ok: bool
    stage: str
    code: str
    exit_code: int
    markets: int
    instruments: int

    def safe_dict(self) -> dict[str, str | int | bool]:
        return asdict(self)


def sync_instruments(
    credentials: Credentials,
    database_path: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> InstrumentSyncResult:
    with TossClient(transport=transport) as client:
        try:
            token_response = client.issue_token(credentials)
        except httpx.RequestError as error:
            raise InstrumentSyncError("network_error", EXIT_NETWORK, stage="token") from error
        _raise_for_response("token", token_response)
        access_token = parse_access_token(token_response.body)
        if access_token is None:
            raise InstrumentSyncError("invalid_token_response", EXIT_REMOTE, stage="token")

        catalog: dict[str, list[Instrument]] = {}
        sync_time = now().isoformat()
        for index, market in enumerate(MARKETS):
            response = _get_market(client, access_token, market, sleeper)
            _raise_for_response("stock_catalog", response, market=market)
            instruments = _parse_instruments(market, response.body)
            if instruments is None:
                raise InstrumentSyncError(
                    "invalid_stock_catalog_response",
                    EXIT_REMOTE,
                    stage="stock_catalog",
                    market=market,
                )
            catalog[market] = instruments
            if index < len(MARKETS) - 1:
                sleeper(_rate_delay(response))
        replace_instrument_catalog(database_path, catalog, sync_time)

    return InstrumentSyncResult(
        ok=True,
        stage="complete",
        code="instrument_sync_ok",
        exit_code=0,
        markets=len(MARKETS),
        instruments=sum(len(items) for items in catalog.values()),
    )


def _parse_instruments(market: str, body: Any) -> list[Instrument] | None:
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        return None
    country = "US" if market in US_MARKETS else "KR"
    parsed: list[Instrument] = []
    seen: set[str] = set()
    for item in body["result"]:
        if not isinstance(item, dict):
            return None
        symbol = item.get("symbol")
        name = item.get("name")
        security_type = item.get("securityType")
        is_common_share = item.get("isCommonShare")
        isin_code = item.get("isinCode")
        if (
            not isinstance(symbol, str)
            or not symbol
            or not isinstance(name, str)
            or not name
            or not isinstance(security_type, str)
            or not isinstance(is_common_share, bool)
            or not isinstance(isin_code, str)
            or not isin_code
        ):
            return None
        normalized_key = symbol.casefold()
        if normalized_key in seen:
            return None
        seen.add(normalized_key)
        if security_type not in SUPPORTED_SECURITY_TYPES:
            continue
        parsed.append(
            Instrument(
                market=market,
                symbol=symbol,
                name=name,
                security_type=security_type,
                is_common_share=is_common_share,
                country=country,
            )
        )
    return parsed


def _raise_for_response(stage: str, response: TossResponse, market: str | None = None) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    provider_code = _provider_code(stage, response.body)
    if status == 401:
        if stage == "token":
            code = "invalid_credentials"
        else:
            code = {
                "expired-token": "token_expired",
                "token-revoked": "token_revoked",
            }.get(provider_code, "invalid_token")
        exit_code = EXIT_AUTHENTICATION
    elif status == 403:
        if stage == "token":
            code = "public_ip_not_allowed"
        else:
            code = "edge_blocked" if provider_code == "edge-blocked" else "stock_catalog_forbidden"
        exit_code = EXIT_PUBLIC_IP if stage == "token" else EXIT_FORBIDDEN
    elif status == 429:
        code = "rate_limited"
        exit_code = EXIT_RATE_LIMIT
    else:
        code = "maintenance" if provider_code == "maintenance" else "remote_error"
        exit_code = EXIT_REMOTE
    raise InstrumentSyncError(
        code,
        exit_code,
        stage=stage,
        market=market,
        http_status=status,
        provider_code=provider_code,
        request_id=response.headers.get("X-Request-Id"),
        retry_after_seconds=response.headers.get("Retry-After"),
    )


def _get_market(
    client: TossClient,
    access_token: str,
    market: str,
    sleeper: Callable[[float], None],
) -> TossResponse:
    for attempt in range(2):
        try:
            response = client.get_all_stocks(access_token, market)
        except httpx.RequestError as error:
            raise InstrumentSyncError(
                "network_error", EXIT_NETWORK, stage="stock_catalog", market=market
            ) from error
        if response.status_code != 429 or attempt == 1:
            return response
        sleeper(_retry_delay(response))
    raise AssertionError("unreachable")


def _provider_code(stage: str, body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if stage == "token":
        return error if isinstance(error, str) and error else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) and code else None


def _rate_delay(response: TossResponse) -> float:
    reset = response.headers.get("X-RateLimit-Reset")
    try:
        return max(float(reset), 1.0) if reset is not None else 1.0
    except ValueError:
        return 1.0


def _retry_delay(response: TossResponse) -> float:
    value = response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset")
    try:
        return min(max(float(value), 1.0), 60.0) if value is not None else 1.0
    except ValueError:
        return 1.0
