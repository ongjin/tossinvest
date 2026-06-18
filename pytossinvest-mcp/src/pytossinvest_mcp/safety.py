from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from typing import Callable

from pytossinvest.money import to_decimal

from .config import Settings
from .stores import TokenStore, SpendStore

_KST = ZoneInfo("Asia/Seoul")

HIGH_VALUE_THRESHOLD = Decimal("100000000")    # 1억 KRW: requires explicit confirm
MAX_ORDER_THRESHOLD = Decimal("3000000000")    # 30억 KRW: always rejected
HIGH_VALUE_THRESHOLD_USD = Decimal("100000")   # $100k: requires explicit confirm
MAX_ORDER_THRESHOLD_USD = Decimal("3000000")   # $3M: always rejected


def order_currency(symbol: str) -> str:
    """Order currency by symbol shape: alphabetic = USD, numeric = KRW (no FX)."""
    return "USD" if symbol.isalpha() else "KRW"


def _canon_symbol(s: str) -> str:
    """Canonicalize a symbol for deny/allow matching: NFKC-fold, drop separator/control chars, uppercase."""
    s = unicodedata.normalize("NFKC", s)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] not in ("Z", "C")).upper()


class GuardrailError(Exception):
    """An order rejected by a client-side safety guardrail (code-based)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _guard_store(fn):
    """Wrap a store I/O call: re-raise connection/timeout/OS errors as GuardrailError.

    Does NOT catch GuardrailError — guardrail rejections (invalid/expired tokens, daily
    limit, etc.) must propagate unchanged.  redis-py's ConnectionError is a subclass of
    the builtin, so no redis import is needed here.
    """
    try:
        return fn()
    except (ConnectionError, TimeoutError, OSError) as exc:
        raise GuardrailError(
            "state-unavailable",
            f"order state store is unavailable: {exc}",
        ) from exc


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
    currency: str
    modify_order_id: "str | None" = None
    prev_notional: "Decimal | None" = None


class SafetyManager:
    def __init__(
        self,
        config: Settings,
        *,
        now: Callable[[], float],
        today: Callable[[], date],
        gen_id: "Callable[[], str] | None" = None,
        token_store: TokenStore,
        spend_store: SpendStore,
    ):
        self._cfg = config
        self._now = now          # monotonic seconds (token expiry)
        self._today = today      # date (daily-cap reset)
        self._gen_id = gen_id or (lambda: uuid.uuid4().hex[:32])
        self.token_store = token_store
        self.spend_store = spend_store

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
        modify_order_id: "str | None" = None,
        currency: "str | None" = None,
    ) -> OrderSpec:
        for label, val in (("quantity", quantity), ("price", price), ("order_amount", order_amount)):
            if val is not None and to_decimal(val) <= 0:
                raise GuardrailError(
                    "invalid-order-value", f"{label} must be a positive number, got {val!r}"
                )
        if order_amount is not None and (price is not None or quantity is not None):
            raise GuardrailError(
                "invalid-order-params",
                "order_amount cannot be combined with price or quantity",
            )
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
            client_order_id=self._gen_id(), currency=currency if currency is not None else order_currency(symbol),
            modify_order_id=modify_order_id,
        )

    def _daily_cap(self, currency: str) -> Decimal:
        cfg = self._cfg
        return to_decimal(cfg.daily_order_limit_usd if currency == "USD" else cfg.daily_order_limit)

    def _delta(self, spec: OrderSpec) -> Decimal:
        if spec.prev_notional is None:
            return spec.notional
        return spec.notional - spec.prev_notional

    def check_guardrails(
        self, spec: OrderSpec, *, is_market_open: bool, enforce_hours: bool,
        check_daily: bool = True, prev_notional: "Decimal | None" = None,
    ) -> None:
        cfg = self._cfg
        if spec.currency == "USD":
            high_value = HIGH_VALUE_THRESHOLD_USD
            hard_ceiling = MAX_ORDER_THRESHOLD_USD
            per_order_cap = to_decimal(cfg.max_order_amount_usd)
            daily_cap = to_decimal(cfg.daily_order_limit_usd)
        else:
            high_value = HIGH_VALUE_THRESHOLD
            hard_ceiling = MAX_ORDER_THRESHOLD
            per_order_cap = to_decimal(cfg.max_order_amount)
            daily_cap = to_decimal(cfg.daily_order_limit)
        sym = _canon_symbol(spec.symbol)
        if cfg.deny_symbols and sym in {_canon_symbol(s) for s in cfg.deny_symbols}:
            raise GuardrailError("symbol-denied", f"{spec.symbol} is in the deny list")
        if cfg.allow_symbols and sym not in {_canon_symbol(s) for s in cfg.allow_symbols}:
            raise GuardrailError("symbol-not-allowed", f"{spec.symbol} is not in the allow list")
        if spec.notional > hard_ceiling:
            raise GuardrailError(
                "max-order-exceeded",
                f"notional {spec.notional} {spec.currency} exceeds the hard {hard_ceiling} ceiling",
            )
        if spec.notional >= high_value and not spec.confirm_high_value_order:
            raise GuardrailError(
                "confirm-high-value-required",
                f"orders >= {high_value} {spec.currency} require confirm_high_value_order=true",
            )
        if spec.notional > per_order_cap:
            raise GuardrailError(
                "order-amount-cap",
                f"notional {spec.notional} {spec.currency} exceeds per-order cap {per_order_cap}",
            )
        if check_daily:
            day = self._today().isoformat()
            increment = spec.notional if prev_notional is None else spec.notional - prev_notional
            if self.spend_store.current(day, spec.currency) + increment > daily_cap:
                raise GuardrailError(
                    "daily-limit",
                    f"this order would push today's {spec.currency} total over the cap",
                )
        if enforce_hours and not is_market_open:
            raise GuardrailError(
                "market-closed",
                "market is closed (set enforce_market_hours=false to override)",
            )

    def reserve(self, spec: OrderSpec) -> bool:
        day = self._today().isoformat()
        return _guard_store(lambda: self.spend_store.reserve(
            day, spec.currency, self._delta(spec), self._daily_cap(spec.currency),
            spec.client_order_id,
        ))

    def release(self, spec: OrderSpec) -> None:
        day = self._today().isoformat()
        _guard_store(lambda: self.spend_store.release(
            day, spec.currency, self._delta(spec), spec.client_order_id,
        ))

    def issue_token(self, spec: OrderSpec) -> str:
        token = self._gen_id()
        now = self._now()
        _guard_store(lambda: self.token_store.put(
            token, spec, expires_at=now + self._cfg.confirmation_ttl_sec, issued_at=now,
        ))
        return token

    def consume(self, token: str) -> OrderSpec:
        rec = _guard_store(lambda: self.token_store.get(token))
        if rec is None:
            raise GuardrailError(
                "invalid-confirmation",
                "unknown or already-used confirmation_token; run preview again",
            )
        spec, expires_at, issued_at = rec
        if self._now() > expires_at:
            _guard_store(lambda: self.token_store.delete(token))
            raise GuardrailError(
                "expired-confirmation",
                "confirmation_token expired; run preview again",
            )
        delay = self._cfg.live_confirm_min_delay_sec
        if self._cfg.is_live and delay > 0 and self._now() - issued_at < delay:
            raise GuardrailError(
                "confirm-too-soon",
                f"live order must wait {delay}s after preview before placing",
            )
        return spec

    def commit(self, token: str) -> None:
        _guard_store(lambda: self.token_store.delete(token))

    def restore_spend(self, events: list[dict]) -> None:
        """Rebuild today's per-currency spend from prior 'placed'/'modified' audit events (UTC ts -> KST date)."""
        today = self._today().isoformat()
        for ev in events:
            if not isinstance(ev, dict) or ev.get("decision") not in ("placed", "modified"):
                continue
            notional = ev.get("notional")
            ts = ev.get("ts")
            if notional is None or ts is None:
                continue
            try:
                ev_date = datetime.fromisoformat(ts).astimezone(_KST).date().isoformat()
                amount = to_decimal(notional)
            except (ValueError, TypeError, InvalidOperation):
                continue
            if ev_date != today:
                continue
            self.spend_store.seed(today, ev.get("currency", "KRW"), amount)
