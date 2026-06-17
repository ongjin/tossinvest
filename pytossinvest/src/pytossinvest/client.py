from __future__ import annotations

import time as _time
from typing import Any, Callable

import httpx

from .auth import TokenManager
from .errors import error_from_response
from .ratelimit import TokenBucket

__all__ = ["TossInvestClient"]

# Base TPS per group (header values override at runtime; these are the documented defaults).
_GROUP_RATES: dict[str, float] = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "ORDER": 6,
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,
}


class TossInvestClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = "https://openapi.tossinvest.com",
        timeout: float = 10.0,
        sleep: Callable[[float], None] = _time.sleep,
        monotonic: Callable[[], float] = _time.monotonic,
    ):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._token = TokenManager(client_id, client_secret, http=self._http, now=monotonic)
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {
            g: TokenBucket(capacity=r, refill_per_sec=r, now=monotonic)
            for g, r in _GROUP_RATES.items()
        }
        self._account_seq: int | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TossInvestClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _gate(self, group: str) -> None:
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        while not bucket.try_acquire():
            self._sleep(bucket.time_until_available())

    def _request(
        self,
        method: str,
        path: str,
        *,
        group: str,
        account: bool = False,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
        _retried: bool = False,
    ) -> Any:
        self._gate(group)
        headers = {"Authorization": f"Bearer {self._token.get_token()}"}
        if account:
            if self._account_seq is None:
                raise RuntimeError("account context required but accountSeq not cached; call get_accounts() first")
            headers["X-Tossinvest-Account"] = str(self._account_seq)

        resp = self._http.request(
            method, path, params=params, json=json, data=data, headers=headers
        )

        if resp.status_code == 200:
            return resp.json().get("result")

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if (
            resp.status_code == 401
            and not _retried
            and (body.get("error") or {}).get("code") == "expired-token"
        ):
            self._token.invalidate()
            return self._request(
                method, path, group=group, account=account,
                params=params, json=json, data=data, _retried=True,
            )

        raise error_from_response(resp.status_code, body, resp.headers)
