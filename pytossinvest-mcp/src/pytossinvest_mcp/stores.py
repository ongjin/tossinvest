from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .safety import OrderSpec


class TokenStore(Protocol):
    def put(self, token: str, spec: "OrderSpec", *, expires_at: float, issued_at: float) -> None: ...
    def get(self, token: str) -> "tuple[OrderSpec, float, float] | None": ...
    def delete(self, token: str) -> None: ...


class SpendStore(Protocol):
    def reserve(self, day: str, currency: str, delta: Decimal, cap: Decimal, dedup_key: str) -> bool: ...
    def release(self, day: str, currency: str, delta: Decimal, dedup_key: str) -> None: ...
    def current(self, day: str, currency: str) -> Decimal: ...
    def seed(self, day: str, currency: str, amount: Decimal) -> None: ...


class MemoryTokenStore:
    def __init__(self) -> None:
        self._d: dict[str, tuple] = {}

    def put(self, token, spec, *, expires_at, issued_at):
        self._d[token] = (spec, expires_at, issued_at)

    def get(self, token):
        return self._d.get(token)

    def delete(self, token):
        self._d.pop(token, None)


class MemorySpendStore:
    def __init__(self) -> None:
        self._spent: dict[tuple[str, str], Decimal] = {}
        self._reserved: dict[str, set[str]] = {}

    def reserve(self, day, currency, delta, cap, dedup_key):
        seen = self._reserved.setdefault(day, set())
        if dedup_key in seen:
            return True
        cur = self._spent.get((day, currency), Decimal("0"))
        if cur + delta > cap:
            return False
        self._spent[(day, currency)] = cur + delta
        seen.add(dedup_key)
        return True

    def release(self, day, currency, delta, dedup_key):
        seen = self._reserved.get(day, set())
        if dedup_key not in seen:
            return
        seen.discard(dedup_key)
        cur = self._spent.get((day, currency), Decimal("0"))
        self._spent[(day, currency)] = max(Decimal("0"), cur - delta)

    def current(self, day, currency):
        return self._spent.get((day, currency), Decimal("0"))

    def seed(self, day, currency, amount):
        cur = self._spent.get((day, currency), Decimal("0"))
        self._spent[(day, currency)] = max(Decimal("0"), cur + amount)
