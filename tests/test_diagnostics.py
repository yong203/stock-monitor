from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from stock_monitor.diagnostics import (
    EXIT_AUTHENTICATION,
    EXIT_FORBIDDEN,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_PUBLIC_IP,
    EXIT_RATE_LIMIT,
    EXIT_REMOTE,
    run_toss_diagnostic,
)
from stock_monitor.settings import Credentials

CREDENTIALS = Credentials(client_id="id-private", client_secret="secret-private")
TOKEN_BODY = {"access_token": "token-private", "token_type": "Bearer", "expires_in": 60}


def response(status: int, body: object, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def transport_with(token_response: httpx.Response, price_response: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openapi.tossinvest.com"
        if request.url.path == "/oauth2/token":
            assert request.method == "POST"
            assert "authorization" not in request.headers
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["client_credentials"],
                "client_id": ["id-private"],
                "client_secret": ["secret-private"],
            }
            return token_response
        assert request.method == "GET"
        assert request.url.path == "/api/v1/prices"
        assert request.url.params["symbols"] == "005930"
        assert request.headers["authorization"] == "Bearer token-private"
        assert request.content == b""
        return price_response

    return httpx.MockTransport(handler)


def test_success_reports_safe_auth_and_market_metadata() -> None:
    transport = transport_with(
        response(
            200,
            TOKEN_BODY,
            **{
                "X-Request-Id": "auth-request",
                "X-RateLimit-Limit": "5",
                "X-RateLimit-Remaining": "4",
                "X-RateLimit-Reset": "1",
            },
        ),
        response(
            200,
            {"result": [{"symbol": "005930", "lastPrice": "70000", "currency": "KRW"}]},
            **{
                "X-Request-Id": "market-request",
                "X-RateLimit-Limit": "15",
                "X-RateLimit-Remaining": "14",
                "X-RateLimit-Reset": "1",
            },
        ),
    )

    result = run_toss_diagnostic(CREDENTIALS, transport=transport)
    output = json.dumps(result.safe_dict())

    assert result.ok is True
    assert result.exit_code == EXIT_OK
    assert result.symbol == "005930"
    assert result.auth_request_id == "auth-request"
    assert result.market_data_request_id == "market-request"
    assert result.auth_rate_limit == "5"
    assert result.market_data_rate_limit == "15"
    assert "private" not in output


@pytest.mark.parametrize(
    ("status", "provider_code", "expected_code", "expected_exit"),
    [
        (401, "invalid_client", "invalid_credentials", EXIT_AUTHENTICATION),
        (403, "access_denied", "public_ip_not_allowed", EXIT_PUBLIC_IP),
        (429, "rate_limit_exceeded", "rate_limited", EXIT_RATE_LIMIT),
        (500, "server_error", "remote_error", EXIT_REMOTE),
    ],
)
def test_token_failures_are_classified(
    status: int, provider_code: str, expected_code: str, expected_exit: int
) -> None:
    transport = httpx.MockTransport(
        lambda request: response(status, {"error": provider_code}, **{"X-Request-Id": "auth"})
    )

    result = run_toss_diagnostic(CREDENTIALS, transport=transport)

    assert result.stage == "token"
    assert result.code == expected_code
    assert result.exit_code == expected_exit
    assert result.provider_code == provider_code
    assert result.auth_request_id == "auth"


@pytest.mark.parametrize(
    ("status", "provider_code", "expected_code", "expected_exit"),
    [
        (401, "expired-token", "token_expired", EXIT_AUTHENTICATION),
        (401, "token-revoked", "token_revoked", EXIT_AUTHENTICATION),
        (401, "invalid-token", "invalid_token", EXIT_AUTHENTICATION),
        (403, "forbidden", "market_data_forbidden", EXIT_FORBIDDEN),
        (403, "edge-blocked", "edge_blocked", EXIT_FORBIDDEN),
        (429, "rate-limit-exceeded", "rate_limited", EXIT_RATE_LIMIT),
        (500, "maintenance", "maintenance", EXIT_REMOTE),
        (500, "internal-error", "remote_error", EXIT_REMOTE),
    ],
)
def test_market_data_failures_are_classified(
    status: int, provider_code: str, expected_code: str, expected_exit: int
) -> None:
    headers = {
        "X-Request-Id": "market",
        "X-RateLimit-Limit": "15",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "1",
    }
    if status == 429:
        headers["Retry-After"] = "2"
    transport = transport_with(
        response(200, TOKEN_BODY, **{"X-Request-Id": "auth"}),
        response(status, {"error": {"code": provider_code}}, **headers),
    )

    result = run_toss_diagnostic(CREDENTIALS, transport=transport)

    assert result.stage == "market_data"
    assert result.code == expected_code
    assert result.exit_code == expected_exit
    assert result.provider_code == provider_code
    assert result.auth_request_id == "auth"
    assert result.market_data_request_id == "market"
    assert result.retry_after_seconds == ("2" if status == 429 else None)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"access_token": "token-private", "expires_in": 60},
        {"access_token": "token-private", "token_type": "Basic", "expires_in": 60},
        {"access_token": "token-private", "token_type": "Bearer", "expires_in": 0},
        {"access_token": "token-private", "token_type": "Bearer", "expires_in": True},
    ],
)
def test_invalid_token_success_body_is_remote_error(body: object) -> None:
    transport = transport_with(response(200, body), response(200, {}))

    result = run_toss_diagnostic(CREDENTIALS, transport=transport)

    assert result.stage == "token"
    assert result.code == "invalid_token_response"
    assert result.exit_code == EXIT_REMOTE


@pytest.mark.parametrize("last_price", ["", "NaN", "not-a-number", "1" * 31])
def test_invalid_price_is_remote_error(last_price: str) -> None:
    transport = transport_with(
        response(200, TOKEN_BODY),
        response(
            200,
            {"result": [{"symbol": "005930", "lastPrice": last_price, "currency": "KRW"}]},
        ),
    )

    result = run_toss_diagnostic(CREDENTIALS, transport=transport)

    assert result.stage == "market_data"
    assert result.code == "invalid_price_response"


def test_unknown_currency_is_forward_compatible() -> None:
    transport = transport_with(
        response(200, TOKEN_BODY),
        response(
            200,
            {"result": [{"symbol": "005930", "lastPrice": "1.2", "currency": "NEW"}]},
        ),
    )

    result = run_toss_diagnostic(CREDENTIALS, transport=transport)

    assert result.ok is True
    assert result.currency == "NEW"


@pytest.mark.parametrize("failed_path", ["/oauth2/token", "/api/v1/prices"])
def test_network_error_preserves_stage(failed_path: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == failed_path:
            raise httpx.ConnectError("secret-private", request=request)
        return response(200, TOKEN_BODY, **{"X-Request-Id": "auth"})

    result = run_toss_diagnostic(CREDENTIALS, transport=httpx.MockTransport(handler))

    expected_stage = "token" if failed_path == "/oauth2/token" else "market_data"
    assert result.stage == expected_stage
    assert result.exit_code == EXIT_NETWORK
    assert "secret-private" not in json.dumps(result.safe_dict())
