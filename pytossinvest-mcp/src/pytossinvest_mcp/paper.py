from __future__ import annotations

import contextlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

from pytossinvest.money import to_decimal


class PaperError(Exception):
    """Paper-broker rule violation (insufficient cash/quantity, bad side)."""


def _as_cash_dict(v) -> dict[str, Decimal]:
    """Normalize a starting-cash arg to {currency: Decimal}. A scalar -> {'KRW': scalar}."""
    if isinstance(v, dict):
        return {str(k): to_decimal(x) for k, x in v.items()}
    return {"KRW": to_decimal(v)}


@dataclass
class Position:
    quantity: Decimal
    avg_price: Decimal
    currency: str


@dataclass
class PaperOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    status: str
    client_order_id: "str | None" = None


@dataclass
class PaperState:
    cash: dict[str, Decimal]
    positions: dict[str, Position]
    orders: list[PaperOrder]
    realized_pnl: dict[str, Decimal]
    counter: int


class PaperStore(Protocol):
    def lock(self): ...
    def load(self) -> PaperState: ...
    def save(self, state: PaperState) -> None: ...


class MemoryPaperStore:
    def __init__(self, *, starting_cash: "str | int | Decimal | dict" = "10000000"):
        cash = _as_cash_dict(starting_cash)
        self._state = PaperState(
            cash=cash, positions={}, orders=[],
            realized_pnl={cur: Decimal("0") for cur in cash}, counter=0,
        )

    def lock(self):
        return contextlib.nullcontext()

    def load(self) -> PaperState:
        return self._state

    def save(self, state: PaperState) -> None:
        self._state = state


class PaperBroker:
    def __init__(self, store: PaperStore, *, next_id: "Callable[[], str] | None" = None):
        self._store = store
        self._next_id = next_id

    def _make_id(self, state: PaperState) -> str:
        if self._next_id is not None:
            return self._next_id()
        state.counter += 1
        return f"paper-{state.counter}"

    def buying_power(self, currency: str) -> Decimal:
        return self._store.load().cash.get(currency, Decimal("0"))

    def sellable_quantity(self, symbol: str) -> Decimal:
        pos = self._store.load().positions.get(symbol)
        return pos.quantity if pos else Decimal("0")

    def place(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        fill_price: "str | int | Decimal",
        quantity: "str | int | Decimal",
        currency: str,
        client_order_id: "str | None" = None,
    ) -> PaperOrder:
        with self._store.lock():
            state = self._store.load()
            if client_order_id is not None:
                existing = next((o for o in state.orders
                                 if o.client_order_id == client_order_id), None)
                if existing is not None:
                    return existing  # idempotent: no second fill
            price = to_decimal(fill_price)
            qty = to_decimal(quantity)
            if side == "BUY":
                cost = price * qty
                have = state.cash.get(currency, Decimal("0"))
                if cost > have:
                    raise PaperError(f"insufficient {currency} cash: need {cost}, have {have}")
                state.cash[currency] = have - cost
                pos = state.positions.get(symbol)
                if pos:
                    total = pos.quantity + qty
                    pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total
                    pos.quantity = total
                else:
                    state.positions[symbol] = Position(
                        quantity=qty, avg_price=price, currency=currency)
            elif side == "SELL":
                pos = state.positions.get(symbol)
                if pos is None or pos.quantity < qty:
                    have = pos.quantity if pos else Decimal("0")
                    raise PaperError(f"insufficient quantity: need {qty}, have {have}")
                cur = pos.currency  # the position's currency is authoritative for the bucket
                state.realized_pnl[cur] = (
                    state.realized_pnl.get(cur, Decimal("0")) + (price - pos.avg_price) * qty)
                state.cash[cur] = state.cash.get(cur, Decimal("0")) + price * qty
                pos.quantity -= qty
                if pos.quantity == 0:
                    del state.positions[symbol]
            else:
                raise PaperError(f"unknown side: {side}")

            order = PaperOrder(
                order_id=self._make_id(state), symbol=symbol, side=side,
                order_type=order_type, quantity=qty, price=price, status="FILLED",
                client_order_id=client_order_id,
            )
            state.orders.append(order)
            self._store.save(state)
            return order

    def get_order(self, order_id: str) -> "PaperOrder | None":
        return next((o for o in self._store.load().orders if o.order_id == order_id), None)

    def list_orders(self) -> list[PaperOrder]:
        return list(self._store.load().orders)

    def holdings(self) -> dict:
        state = self._store.load()
        return {
            "cash": {cur: str(amt) for cur, amt in state.cash.items()},
            "realizedPnl": {cur: str(amt) for cur, amt in state.realized_pnl.items()},
            "items": [
                {"symbol": s, "currency": p.currency, "quantity": str(p.quantity),
                 "averagePurchasePrice": str(p.avg_price)}
                for s, p in state.positions.items()
            ],
        }
