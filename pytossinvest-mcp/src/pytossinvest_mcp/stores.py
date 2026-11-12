from __future__ import annotations

import threading
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .safety import OrderSpec


class TokenStore(Protocol):
    def put(self, token: str, spec: "OrderSpec", *, expires_at: float, issued_at: float) -> None: ...
    def get(self, token: str) -> "tuple[OrderSpec, float, float] | None": ...
    def delete(self, token: str) -> None: ...
    def claim(self, token: str) -> bool: ...  # False while another call is executing the token
    def unclaim(self, token: str) -> None: ...


class SpendStore(Protocol):
    def reserve(self, day: str, currency: str, delta: Decimal, cap: Decimal, dedup_key: str) -> bool: ...
    def release(self, day: str, currency: str, delta: Decimal, dedup_key: str) -> None: ...
    def reserved(self, day: str, dedup_key: str) -> bool: ...
    def current(self, day: str, currency: str) -> Decimal: ...
    def seed(self, day: str, currency: str, amount: Decimal) -> None: ...


# The MCP SDK runs each sync tool in a worker thread, so parallel tool calls reach these stores
# at once: every check-then-act sequence below holds a lock.
class MemoryTokenStore:
    def __init__(self) -> None:
        self._d: dict[str, tuple] = {}
        self._executing: set[str] = set()
        self._lock = threading.Lock()

    def put(self, token, spec, *, expires_at, issued_at):
        self._d[token] = (spec, expires_at, issued_at)

    def get(self, token):
        return self._d.get(token)

    def delete(self, token):
        self._d.pop(token, None)

    def claim(self, token):
        with self._lock:
            if token in self._executing:
                return False
            self._executing.add(token)
            return True

    def unclaim(self, token):
        with self._lock:
            self._executing.discard(token)


class MemorySpendStore:
    def __init__(self) -> None:
        self._spent: dict[tuple[str, str], Decimal] = {}
        self._reserved: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def reserve(self, day, currency, delta, cap, dedup_key):
        with self._lock:
            seen = self._reserved.setdefault(day, set())
            if dedup_key in seen:
                return True
            cur = self._spent.get((day, currency), Decimal("0"))
            if cur + delta > cap:
                return False
            # floored: a downsize of an order never counted here must not bank credit under the cap
            self._spent[(day, currency)] = max(Decimal("0"), cur + delta)
            seen.add(dedup_key)
            return True

    def release(self, day, currency, delta, dedup_key):
        with self._lock:
            seen = self._reserved.get(day, set())
            if dedup_key not in seen:
                return
            seen.discard(dedup_key)
            cur = self._spent.get((day, currency), Decimal("0"))
            self._spent[(day, currency)] = max(Decimal("0"), cur - delta)

    def reserved(self, day, dedup_key):
        with self._lock:
            return dedup_key in self._reserved.get(day, ())

    def current(self, day, currency):
        return self._spent.get((day, currency), Decimal("0"))

    def seed(self, day, currency, amount):
        with self._lock:
            cur = self._spent.get((day, currency), Decimal("0"))
            self._spent[(day, currency)] = max(Decimal("0"), cur + amount)
