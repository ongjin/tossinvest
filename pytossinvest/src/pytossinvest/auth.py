from __future__ import annotations

import threading
import time as _time
from typing import Callable

import httpx

from .errors import OAuthError, oauth_error_from_response

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
        # Held across the fetch on purpose: the API keeps one live token per client, so two
        # concurrent issues would revoke each other. Waiters reuse the first one's token.
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token is not None and self._now() < self._expires_at:
                return self._token
            return self._fetch()

    def invalidate(self, token: str | None = None) -> None:
        """Drop the cached token. Given the token a request failed with, drop it only if it is
        still the cached one, so a late 401 cannot discard a token re-issued in the meantime."""
        with self._lock:
            if token is not None and token != self._token:
                return
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
        try:
            body = resp.json()
            token = body["access_token"]
            ttl = float(body["expires_in"])
        except (ValueError, KeyError, TypeError):
            raise OAuthError(
                "invalid-response", "200 token response was malformed", http_status=200
            ) from None

        self._token = token
        self._expires_at = self._now() + max(0.0, ttl - _EXPIRY_BUFFER_SEC)
        return self._token
