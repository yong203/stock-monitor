from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .settings import Credentials

BASE_URL = "https://openapi.tossinvest.com"
PROBE_SYMBOL = "005930"


@dataclass(frozen=True)
class TossResponse:
    status_code: int
    body: Any = field(repr=False)
    headers: httpx.Headers


class TossClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TossClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def issue_token(self, credentials: Credentials) -> TossResponse:
        response = self._client.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _response(response)

    def get_probe_price(self, access_token: str) -> TossResponse:
        response = self._client.get(
            "/api/v1/prices",
            params={"symbols": PROBE_SYMBOL},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return _response(response)

    def get_all_stocks(self, access_token: str, market: str) -> TossResponse:
        response = self._client.get(
            "/api/v1/stocks/all",
            params={"market": market, "status": "ACTIVE"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return _response(response)


def parse_access_token(body: Any) -> str | None:
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


def _response(response: httpx.Response) -> TossResponse:
    try:
        body = response.json()
    except ValueError:
        body = None
    return TossResponse(response.status_code, body, response.headers)
