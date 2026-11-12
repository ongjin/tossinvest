"""Shares the broker's access token between MCP instances that run behind one redis."""
from __future__ import annotations

import base64
import contextlib
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .safety import _STORE_ERRORS

_KEY_CONTEXT = b"pytossinvest-mcp access-token cache v1"

# The lock is held while one instance issues a token: a single HTTP call. An instance that
# cannot get the lock in that time issues on its own. Kept short because the wait happens
# inside an API call, order sends included.
LOCK_TIMEOUT_SEC = 15.0

MS_PER_SEC = 1000


class RedisTokenCache:
    """pytossinvest's TokenCache on redis.

    The API keeps one live token per client id, so instances that each issued their own would
    keep revoking one another. Here the first one issues and the rest adopt what it stored.

    The token is encrypted with a key derived from the client secret, which redis never holds:
    read access to redis must not amount to API access that bypasses every guardrail.

    Any redis failure degrades to "no sharing" (each instance issues its own token, as it does
    without a cache) instead of taking every API call down with it."""

    def __init__(self, client, *, client_id: str, client_secret: str):
        self._r = client
        owner = hashlib.sha256(client_id.encode()).hexdigest()[:16]
        self._key = f"oauth:token:{owner}"
        self._lock_key = f"lock:oauth:token:{owner}"
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=client_id.encode(), info=_KEY_CONTEXT,
        ).derive(client_secret.encode())
        self._cipher = Fernet(base64.urlsafe_b64encode(key))

    @contextlib.contextmanager
    def lock(self):
        lock = self._r.lock(
            self._lock_key, timeout=LOCK_TIMEOUT_SEC, blocking_timeout=LOCK_TIMEOUT_SEC,
        )
        held = False
        try:
            held = lock.acquire()
        except _STORE_ERRORS:
            pass

        try:
            yield
        finally:
            if held:
                try:
                    lock.release()
                except _STORE_ERRORS:
                    pass  # it expired or redis went away; either way it is no longer ours

    def load(self) -> "tuple[str, float] | None":
        try:
            pipe = self._r.pipeline()
            pipe.get(self._key)
            pipe.pttl(self._key)
            stored, ttl_ms = pipe.execute()
        except _STORE_ERRORS:
            return None

        token = self._decrypt(stored)
        if token is None or ttl_ms <= 0:
            return None
        return token, ttl_ms / MS_PER_SEC

    def save(self, token: str, ttl_sec: float) -> None:
        sealed = self._cipher.encrypt(token.encode()).decode()
        try:
            self._r.set(self._key, sealed, px=int(ttl_sec * MS_PER_SEC))
        except _STORE_ERRORS:
            pass

    def discard(self, token: str) -> None:
        def step(pipe) -> None:
            if self._decrypt(pipe.get(self._key)) != token:
                return  # another instance already replaced it
            pipe.multi()
            pipe.delete(self._key)

        try:
            self._r.transaction(step, self._key)
        except _STORE_ERRORS:
            pass

    def _decrypt(self, stored) -> "str | None":
        if stored is None:
            return None
        sealed = stored.encode() if isinstance(stored, str) else stored
        try:
            return self._cipher.decrypt(sealed).decode()
        except InvalidToken:
            return None  # written with another secret, or not ours
