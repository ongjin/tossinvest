from __future__ import annotations

import json
from decimal import Decimal

from pytossinvest.money import to_decimal

from .paper import PaperState, Position, PaperOrder
from .safety import OrderSpec


def _spec_to_dict(spec: OrderSpec) -> dict:
    return {
        "symbol": spec.symbol,
        "side": spec.side,
        "order_type": spec.order_type,
        "quantity": spec.quantity,
        "price": spec.price,
        "order_amount": spec.order_amount,
        "time_in_force": spec.time_in_force,
        "confirm_high_value_order": spec.confirm_high_value_order,
        "notional": str(spec.notional),
        "client_order_id": spec.client_order_id,
        "currency": spec.currency,
        "modify_order_id": spec.modify_order_id,
        "prev_notional": None if spec.prev_notional is None else str(spec.prev_notional),
    }


def _spec_from_dict(d: dict) -> OrderSpec:
    return OrderSpec(
        symbol=d["symbol"],
        side=d["side"],
        order_type=d["order_type"],
        quantity=d["quantity"],
        price=d["price"],
        order_amount=d["order_amount"],
        time_in_force=d["time_in_force"],
        confirm_high_value_order=d["confirm_high_value_order"],
        notional=to_decimal(d["notional"]),
        client_order_id=d["client_order_id"],
        currency=d["currency"],
        modify_order_id=d["modify_order_id"],
        prev_notional=None if d["prev_notional"] is None else to_decimal(d["prev_notional"]),
    )


class RedisTokenStore:
    def __init__(self, client, *, prefix: str = "tok:", grace_sec: int = 86400):
        self._r = client
        self._prefix = prefix
        self._grace = grace_sec  # physical TTL; code checks expires_at for logical expiry

    def _key(self, token: str) -> str:
        return f"{self._prefix}{token}"

    def put(self, token: str, spec: OrderSpec, *, expires_at: float, issued_at: float) -> None:
        payload = json.dumps({
            "spec": _spec_to_dict(spec),
            "expires_at": expires_at,
            "issued_at": issued_at,
        })
        self._r.set(self._key(token), payload, ex=self._grace)

    def get(self, token: str):
        raw = self._r.get(self._key(token))
        if raw is None:
            return None
        d = json.loads(raw)
        return _spec_from_dict(d["spec"]), d["expires_at"], d["issued_at"]

    def delete(self, token: str) -> None:
        self._r.delete(self._key(token))


class RedisSpendStore:
    def __init__(self, client, *, lock_timeout: float = 5.0, ttl_sec: int = 172800):
        self._r = client
        self._lock_timeout = lock_timeout
        self._ttl = ttl_sec  # 2-day GC backstop; keys roll by day

    def _spend_key(self, day: str, currency: str) -> str:
        return f"spend:{day}:{currency}"

    def _reserved_key(self, day: str) -> str:
        return f"reserved:{day}"

    def _lock(self, day: str):
        return self._r.lock(
            f"lock:spend:{day}",
            timeout=self._lock_timeout,
            blocking_timeout=self._lock_timeout,
        )

    def reserve(self, day: str, currency: str, delta: Decimal, cap: Decimal, dedup_key: str) -> bool:
        with self._lock(day):
            if self._r.sismember(self._reserved_key(day), dedup_key):
                return True
            cur = to_decimal(self._r.get(self._spend_key(day, currency)) or "0")
            if cur + delta > cap:
                return False
            self._r.set(self._spend_key(day, currency), str(cur + delta), ex=self._ttl)
            self._r.sadd(self._reserved_key(day), dedup_key)
            self._r.expire(self._reserved_key(day), self._ttl)
            return True

    def release(self, day: str, currency: str, delta: Decimal, dedup_key: str) -> None:
        with self._lock(day):
            if not self._r.sismember(self._reserved_key(day), dedup_key):
                return
            self._r.srem(self._reserved_key(day), dedup_key)
            cur = to_decimal(self._r.get(self._spend_key(day, currency)) or "0")
            new = cur - delta
            if new < Decimal("0"):
                new = Decimal("0")
            self._r.set(self._spend_key(day, currency), str(new), ex=self._ttl)

    def current(self, day: str, currency: str) -> Decimal:
        return to_decimal(self._r.get(self._spend_key(day, currency)) or "0")

    def seed(self, day: str, currency: str, amount: Decimal) -> None:
        # Redis counters survive restarts; re-seeding from audit would double-count. No-op.
        return None


def _paper_state_to_dict(state: PaperState) -> dict:
    return {
        "cash": str(state.cash),
        "realized_pnl": str(state.realized_pnl),
        "counter": state.counter,
        "positions": {
            s: {"quantity": str(p.quantity), "avg_price": str(p.avg_price)}
            for s, p in state.positions.items()
        },
        "orders": [
            {"order_id": o.order_id, "symbol": o.symbol, "side": o.side,
             "order_type": o.order_type, "quantity": str(o.quantity),
             "price": str(o.price), "status": o.status,
             "client_order_id": o.client_order_id}
            for o in state.orders
        ],
    }


def _paper_state_from_dict(d: dict) -> PaperState:
    return PaperState(
        cash=to_decimal(d["cash"]),
        realized_pnl=to_decimal(d["realized_pnl"]),
        counter=d["counter"],
        positions={
            s: Position(quantity=to_decimal(p["quantity"]), avg_price=to_decimal(p["avg_price"]))
            for s, p in d["positions"].items()
        },
        orders=[
            PaperOrder(
                order_id=o["order_id"], symbol=o["symbol"], side=o["side"],
                order_type=o["order_type"], quantity=to_decimal(o["quantity"]),
                price=to_decimal(o["price"]), status=o["status"],
                client_order_id=o["client_order_id"],
            )
            for o in d["orders"]
        ],
    )


class RedisPaperStore:
    def __init__(self, client, *, starting_cash, key: str = "paper", lock_timeout: float = 5.0):
        self._r = client
        self._key = key
        self._starting = to_decimal(starting_cash)
        self._lock_timeout = lock_timeout

    def lock(self):
        return self._r.lock(f"lock:{self._key}", timeout=self._lock_timeout,
                            blocking_timeout=self._lock_timeout)

    def load(self) -> PaperState:
        raw = self._r.get(self._key)
        if raw is None:
            return PaperState(cash=self._starting, positions={}, orders=[],
                              realized_pnl=Decimal("0"), counter=0)
        return _paper_state_from_dict(json.loads(raw))

    def save(self, state: PaperState) -> None:
        self._r.set(self._key, json.dumps(_paper_state_to_dict(state)))
