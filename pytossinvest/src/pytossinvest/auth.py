from __future__ import annotations

import threading
import time as _time
from typing import Callable, ContextManager, Protocol

import httpx

from .errors import OAuthError, oauth_error_from_response

__all__ = ["TokenManager", "TokenCache"]

_EXPIRY_BUFFER_SEC = 30.0


class TokenCache(Protocol):
    """Where processes that use one client id share its token. The API keeps a single live
    token per client: every issue revokes the one before, so processes that each issue their
    own keep invalidating one another. With a shared cache only one of them issues."""

    def lock(self) -> ContextManager:
        """Held while loading and, on a miss, issuing: one issue at a time across processes."""

    def load(self) -> "tuple[str, float] | None":
        """The shared token and the seconds it stays usable, or None."""

    def save(self, token: str, ttl_sec: float) -> None: ...

    def discard(self, token: str) -> None:
        """Remove the shared token, but only if it is still this one."""


class TokenManager:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.Client,
        now: Callable[[], float] = _time.monotonic,
        cache: TokenCache | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._now = now
        self._cache = cache
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Tokens the API refused are never adopted from the shared cache again: the last one
        # refused, and the entry a cache keeps handing out when it cannot be written to.
        self._rejected: str | None = None
        self._stuck: str | None = None
        # Held across the fetch on purpose: the API keeps one live token per client, so two
        # concurrent issues would revoke each other. Waiters reuse the first one's token.
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token is not None and self._now() < self._expires_at:
                return self._token
            if self._cache is None:
                return self._fetch()
            return self._load_or_fetch(self._cache)

    def _load_or_fetch(self, cache: TokenCache) -> str:
        with cache.lock():
            shared = cache.load()
            if shared is not None and shared[0] == self._rejected:
                self._stuck = self._rejected
            if shared is not None and shared[0] != self._stuck:
                self._token, ttl_sec = shared
                self._expires_at = self._now() + ttl_sec
                return self._token

            token = self._fetch()
            ttl_sec = self._expires_at - self._now()
            if ttl_sec > 0:
                cache.save(token, ttl_sec)
            return token

    def invalidate(self, token: str | None = None) -> None:
        """Drop the cached token. Given the token a request failed with, drop it only if it is
        still the cached one, so a late 401 cannot discard a token re-issued in the meantime.
        The shared cache follows the same rule: another process may already have replaced it."""
        with self._lock:
            if token is not None and token != self._token:
                return
            stale = self._token
            self._token = None
            self._expires_at = 0.0
            self._rejected = stale
            if self._cache is not None and stale is not None:
                self._cache.discard(stale)

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
