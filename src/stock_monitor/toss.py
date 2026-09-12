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


def _response(response: httpx.Response) -> TossResponse:
    try:
        body = response.json()
    except ValueError:
        body = None
    return TossResponse(response.status_code, body, response.headers)
