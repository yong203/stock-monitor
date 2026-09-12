from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .settings import Credentials
from .toss import PROBE_SYMBOL, TossClient, TossResponse

EXIT_OK = 0
EXIT_AUTHENTICATION = 10
EXIT_PUBLIC_IP = 11
EXIT_RATE_LIMIT = 12
EXIT_NETWORK = 13
EXIT_REMOTE = 14
EXIT_FORBIDDEN = 15


@dataclass(frozen=True)
class DiagnosticResult:
    ok: bool
    stage: str
    code: str
    exit_code: int
    http_status: int | None = None
    provider_code: str | None = None
    auth_request_id: str | None = None
    auth_rate_limit: str | None = None
    auth_rate_remaining: str | None = None
    auth_rate_reset_seconds: str | None = None
    market_data_request_id: str | None = None
    market_data_rate_limit: str | None = None
    market_data_rate_remaining: str | None = None
    market_data_rate_reset_seconds: str | None = None
    retry_after_seconds: str | None = None
    symbol: str | None = None
    currency: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def run_toss_diagnostic(
    credentials: Credentials,
    *,
    transport: httpx.BaseTransport | None = None,
) -> DiagnosticResult:
    with TossClient(transport=transport) as client:
        try:
            token_response = client.issue_token(credentials)
        except httpx.RequestError:
            return _network_failure("token")

        token_failure = _failure("token", token_response)
        if token_failure:
            return token_failure

        access_token = _access_token(token_response.body)
        if access_token is None:
            return _remote_failure("token", token_response, "invalid_token_response")

        try:
            price_response = client.get_probe_price(access_token)
        except httpx.RequestError:
            return DiagnosticResult(
                ok=False,
                stage="market_data",
                code="network_error",
                exit_code=EXIT_NETWORK,
                **_metadata("auth", token_response),
            )

        price_failure = _failure("market_data", price_response, auth_response=token_response)
        if price_failure:
            return price_failure

        quote = _probe_quote(price_response.body)
        if quote is None:
            return _remote_failure(
                "market_data",
                price_response,
                "invalid_price_response",
                auth_response=token_response,
            )

        return DiagnosticResult(
            ok=True,
            stage="complete",
            code="toss_connection_ok",
            exit_code=EXIT_OK,
            symbol=quote["symbol"],
            currency=quote["currency"],
            **_metadata("auth", token_response),
            **_metadata("market_data", price_response),
        )


def _failure(
    stage: str,
    response: TossResponse,
    *,
    auth_response: TossResponse | None = None,
) -> DiagnosticResult | None:
    status = response.status_code
    if 200 <= status < 300:
        return None

    provider_code = _provider_code(stage, response.body)
    metadata = _metadata(stage, response)
    if auth_response is not None:
        metadata.update(_metadata("auth", auth_response))
    return DiagnosticResult(
        ok=False,
        stage=stage,
        code=_diagnostic_code(stage, status, provider_code),
        exit_code=_exit_code(stage, status),
        http_status=status,
        provider_code=provider_code,
        **metadata,
    )


def _remote_failure(
    stage: str,
    response: TossResponse,
    code: str,
    *,
    auth_response: TossResponse | None = None,
) -> DiagnosticResult:
    metadata = _metadata(stage, response)
    if auth_response is not None:
        metadata.update(_metadata("auth", auth_response))
    return DiagnosticResult(
        ok=False,
        stage=stage,
        code=code,
        exit_code=EXIT_REMOTE,
        http_status=response.status_code,
        **metadata,
    )


def _network_failure(stage: str) -> DiagnosticResult:
    return DiagnosticResult(
        ok=False,
        stage=stage,
        code="network_error",
        exit_code=EXIT_NETWORK,
    )


def _diagnostic_code(stage: str, status: int, provider_code: str | None) -> str:
    if stage == "token":
        if status == 401:
            return "invalid_credentials"
        if status == 403:
            return "public_ip_not_allowed"
    elif status == 401:
        return {
            "expired-token": "token_expired",
            "token-revoked": "token_revoked",
            "login-user-not-found": "login_user_not_found",
        }.get(provider_code, "invalid_token")
    elif status == 403:
        return "edge_blocked" if provider_code == "edge-blocked" else "market_data_forbidden"

    if status == 429:
        return "rate_limited"
    if status == 500 and provider_code == "maintenance":
        return "maintenance"
    return "remote_error"


def _exit_code(stage: str, status: int) -> int:
    if status == 401:
        return EXIT_AUTHENTICATION
    if status == 403:
        return EXIT_PUBLIC_IP if stage == "token" else EXIT_FORBIDDEN
    if status == 429:
        return EXIT_RATE_LIMIT
    return EXIT_REMOTE


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


def _access_token(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    access_token = body.get("access_token")
    token_type = body.get("token_type")
    expires_in = body.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        return None
    if token_type != "Bearer":
        return None
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        return None
    return access_token


def _probe_quote(body: Any) -> dict[str, str] | None:
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        return None
    for quote in body["result"]:
        if not isinstance(quote, dict) or quote.get("symbol") != PROBE_SYMBOL:
            continue
        if not _valid_decimal(quote.get("lastPrice")):
            continue
        currency = quote.get("currency")
        if not isinstance(currency, str) or not currency:
            continue
        return {"symbol": quote["symbol"], "currency": currency}
    return None


def _valid_decimal(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 30:
        return False
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _metadata(stage: str, response: TossResponse) -> dict[str, str]:
    prefix = "auth" if stage in {"token", "auth"} else "market_data"
    values = {
        f"{prefix}_request_id": response.headers.get("X-Request-Id"),
        f"{prefix}_rate_limit": response.headers.get("X-RateLimit-Limit"),
        f"{prefix}_rate_remaining": response.headers.get("X-RateLimit-Remaining"),
        f"{prefix}_rate_reset_seconds": response.headers.get("X-RateLimit-Reset"),
        "retry_after_seconds": response.headers.get("Retry-After"),
    }
    return {key: value for key, value in values.items() if value is not None}
