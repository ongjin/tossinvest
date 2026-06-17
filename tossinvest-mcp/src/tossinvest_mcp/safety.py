from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from pytossinvest.money import to_decimal

from .config import Settings

HIGH_VALUE_THRESHOLD = Decimal("100000000")    # 1억 KRW: requires explicit confirm
MAX_ORDER_THRESHOLD = Decimal("3000000000")    # 30억 KRW: always rejected


class GuardrailError(Exception):
    """An order rejected by a client-side safety guardrail (code-based)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class OrderSpec:
    symbol: str
    side: str
    order_type: str
    quantity: "str | None"
    price: "str | None"
    order_amount: "str | None"
    time_in_force: str
    confirm_high_value_order: bool
    notional: Decimal
    client_order_id: str


@dataclass
class _Pending:
    spec: OrderSpec
    expires_at: float


class SafetyManager:
    def __init__(
        self,
        config: Settings,
        *,
        now: Callable[[], float],
        today: Callable[[], date],
        gen_id: "Callable[[], str] | None" = None,
    ):
        self._cfg = config
        self._now = now          # monotonic seconds (token expiry)
        self._today = today      # date (daily-cap reset)
        self._gen_id = gen_id or (lambda: uuid.uuid4().hex[:32])
        self._pending: dict[str, _Pending] = {}
        self._spent_date: "date | None" = None
        self._spent: Decimal = Decimal("0")

    def build_spec(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: "str | None" = None,
        price: "str | None" = None,
        order_amount: "str | None" = None,
        time_in_force: str = "DAY",
        confirm_high_value_order: bool = False,
        ref_price: "str | None" = None,
    ) -> OrderSpec:
        if order_amount is not None:
            notional = to_decimal(order_amount)
        elif price is not None and quantity is not None:
            notional = to_decimal(price) * to_decimal(quantity)
        elif quantity is not None and ref_price is not None:
            notional = to_decimal(ref_price) * to_decimal(quantity)
        else:
            raise GuardrailError(
                "insufficient-order-params",
                "need price+quantity, order_amount, or quantity+ref_price",
            )
        return OrderSpec(
            symbol=symbol, side=side, order_type=order_type, quantity=quantity,
            price=price, order_amount=order_amount, time_in_force=time_in_force,
            confirm_high_value_order=confirm_high_value_order, notional=notional,
            client_order_id=self._gen_id(),
        )

    def check_guardrails(
        self, spec: OrderSpec, *, is_market_open: bool, enforce_hours: bool
    ) -> None:
        cfg = self._cfg
        if cfg.deny_symbols and spec.symbol in cfg.deny_symbols:
            raise GuardrailError("symbol-denied", f"{spec.symbol} is in the deny list")
        if cfg.allow_symbols and spec.symbol not in cfg.allow_symbols:
            raise GuardrailError("symbol-not-allowed", f"{spec.symbol} is not in the allow list")
        if spec.notional > MAX_ORDER_THRESHOLD:
            raise GuardrailError(
                "max-order-exceeded",
                f"notional {spec.notional} exceeds the hard 3,000,000,000 ceiling",
            )
        if spec.notional >= HIGH_VALUE_THRESHOLD and not spec.confirm_high_value_order:
            raise GuardrailError(
                "confirm-high-value-required",
                "orders >= 100,000,000 require confirm_high_value_order=true",
            )
        if spec.notional > to_decimal(cfg.max_order_amount):
            raise GuardrailError(
                "order-amount-cap",
                f"notional {spec.notional} exceeds per-order cap {cfg.max_order_amount}",
            )
        self._roll_daily()
        if self._spent + spec.notional > to_decimal(cfg.daily_order_limit):
            raise GuardrailError(
                "daily-limit",
                f"this order would push today's total over {cfg.daily_order_limit}",
            )
        if enforce_hours and not is_market_open:
            raise GuardrailError(
                "market-closed",
                "market is closed (set enforce_market_hours=false to override)",
            )

    def _roll_daily(self) -> None:
        d = self._today()
        if self._spent_date != d:
            self._spent_date = d
            self._spent = Decimal("0")

    def record_spend(self, notional: Decimal) -> None:
        self._roll_daily()
        self._spent += notional

    def issue_token(self, spec: OrderSpec) -> str:
        token = self._gen_id()
        self._pending[token] = _Pending(
            spec=spec, expires_at=self._now() + self._cfg.confirmation_ttl_sec
        )
        return token

    def consume(self, token: str) -> OrderSpec:
        pending = self._pending.get(token)
        if pending is None:
            raise GuardrailError(
                "invalid-confirmation",
                "unknown or already-used confirmation_token; run preview_order again",
            )
        if self._now() > pending.expires_at:
            del self._pending[token]
            raise GuardrailError(
                "expired-confirmation",
                "confirmation_token expired; run preview_order again",
            )
        return pending.spec

    def finalize(self, token: str, notional: Decimal) -> None:
        self._pending.pop(token, None)
        self.record_spend(notional)
