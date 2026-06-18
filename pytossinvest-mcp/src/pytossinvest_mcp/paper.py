from __future__ import annotations

import contextlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

from pytossinvest.money import to_decimal


class PaperError(Exception):
    """Paper-broker rule violation (insufficient cash/quantity, bad side)."""


@dataclass
class Position:
    quantity: Decimal
    avg_price: Decimal


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
    cash: Decimal
    positions: dict[str, Position]
    orders: list[PaperOrder]
    realized_pnl: Decimal
    counter: int


class PaperStore(Protocol):
    def lock(self): ...
    def load(self) -> PaperState: ...
    def save(self, state: PaperState) -> None: ...


class MemoryPaperStore:
    def __init__(self, *, starting_cash: "str | int | Decimal" = "10000000"):
        self._state = PaperState(
            cash=to_decimal(starting_cash), positions={}, orders=[],
            realized_pnl=Decimal("0"), counter=0,
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

    def buying_power(self) -> Decimal:
        return self._store.load().cash

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
                if cost > state.cash:
                    raise PaperError(f"insufficient cash: need {cost}, have {state.cash}")
                state.cash -= cost
                pos = state.positions.get(symbol)
                if pos:
                    total = pos.quantity + qty
                    pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total
                    pos.quantity = total
                else:
                    state.positions[symbol] = Position(quantity=qty, avg_price=price)
            elif side == "SELL":
                pos = state.positions.get(symbol)
                if pos is None or pos.quantity < qty:
                    have = pos.quantity if pos else Decimal("0")
                    raise PaperError(f"insufficient quantity: need {qty}, have {have}")
                state.realized_pnl += (price - pos.avg_price) * qty
                state.cash += price * qty
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
            "cash": str(state.cash),
            "realizedPnl": str(state.realized_pnl),
            "items": [
                {"symbol": s, "quantity": str(p.quantity),
                 "averagePurchasePrice": str(p.avg_price)}
                for s, p in state.positions.items()
            ],
        }
