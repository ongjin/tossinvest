from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

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


class PaperBroker:
    def __init__(
        self,
        *,
        starting_cash: "str | int | Decimal" = "10000000",
        next_id: "Callable[[], str] | None" = None,
    ):
        self.cash: Decimal = to_decimal(starting_cash)
        self.positions: dict[str, Position] = {}
        self.orders: list[PaperOrder] = []
        self.realized_pnl: Decimal = Decimal("0")
        self._counter = 0
        self._next_id = next_id or self._default_id

    def _default_id(self) -> str:
        self._counter += 1
        return f"paper-{self._counter}"

    def buying_power(self) -> Decimal:
        return self.cash

    def sellable_quantity(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
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
        price = to_decimal(fill_price)
        qty = to_decimal(quantity)
        if side == "BUY":
            cost = price * qty
            if cost > self.cash:
                raise PaperError(f"insufficient cash: need {cost}, have {self.cash}")
            self.cash -= cost
            pos = self.positions.get(symbol)
            if pos:
                total = pos.quantity + qty
                pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total
                pos.quantity = total
            else:
                self.positions[symbol] = Position(quantity=qty, avg_price=price)
        elif side == "SELL":
            pos = self.positions.get(symbol)
            if pos is None or pos.quantity < qty:
                have = pos.quantity if pos else Decimal("0")
                raise PaperError(f"insufficient quantity: need {qty}, have {have}")
            self.realized_pnl += (price - pos.avg_price) * qty
            self.cash += price * qty
            pos.quantity -= qty
            if pos.quantity == 0:
                del self.positions[symbol]
        else:
            raise PaperError(f"unknown side: {side}")

        order = PaperOrder(
            order_id=self._next_id(), symbol=symbol, side=side, order_type=order_type,
            quantity=qty, price=price, status="FILLED", client_order_id=client_order_id,
        )
        self.orders.append(order)
        return order

    def get_order(self, order_id: str) -> "PaperOrder | None":
        return next((o for o in self.orders if o.order_id == order_id), None)

    def list_orders(self) -> list[PaperOrder]:
        return list(self.orders)

    def holdings(self) -> dict:
        return {
            "cash": str(self.cash),
            "realizedPnl": str(self.realized_pnl),
            "items": [
                {
                    "symbol": s,
                    "quantity": str(p.quantity),
                    "averagePurchasePrice": str(p.avg_price),
                }
                for s, p in self.positions.items()
            ],
        }
