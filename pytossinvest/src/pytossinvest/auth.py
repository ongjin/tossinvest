from __future__ import annotations

import time as _time
from typing import Callable

import httpx

from .errors import oauth_error_from_response

__all__ = ["TokenManager"]

_EXPIRY_BUFFER_SEC = 30.0


class TokenManager:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.Client,
        now: Callable[[], float] = _time.monotonic,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._now = now
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        if self._token is not None and self._now() < self._expires_at:
            return self._token
        return self._fetch()

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def _fetch(self) -> str:
        resp = self._http.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            try:
                err_body = resp.json()
            except ValueError:
                err_body = {}
            raise oauth_error_from_response(resp.status_code, err_body)
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = self._now() + float(body["expires_in"]) - _EXPIRY_BUFFER_SEC
        return self._token
