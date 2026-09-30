"""Conditional orders (SINGLE / OCO / OTO) over MCP.

Preview tools here issue confirmation tokens whose spec has kind=conditional; placing and
modifying run through tools.place_order / tools.modify_order so the safety path stays single.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from pytossinvest.money import to_decimal

from .paper import PaperError
from .safety import CONDITIONAL_KIND, GuardrailError, _is_positive_number

LIMIT = "LIMIT"
SINGLE = "SINGLE"
OCO = "OCO"


def _leg(side: str, trigger_price: str, order_price: str | None) -> dict:
    leg = {"orderSide": side, "triggerPrice": trigger_price}
    if order_price is not None:
        leg["orderPrice"] = order_price
    return leg


def build_request(*, type: str, quantity: str, order_type: str, expire_date: str,
                  first_side: str, first_trigger_price: str, first_order_price: str | None = None,
                  second_side: str | None = None, second_trigger_price: str | None = None,
                  second_order_price: str | None = None) -> dict:
    """Validate flat tool arguments into SDK create/modify kwargs (without symbol)."""
    numbers = {"quantity": quantity, "first_trigger_price": first_trigger_price,
               "first_order_price": first_order_price, "second_trigger_price": second_trigger_price,
               "second_order_price": second_order_price}
    for label, val in numbers.items():
        if val is not None and not _is_positive_number(val):
            raise GuardrailError("invalid-order-value", f"{label} must be a positive number, got {val!r}")

    try:
        date.fromisoformat(expire_date)
    except ValueError:
        raise GuardrailError("invalid-order-params",
                             f"expire_date must be YYYY-MM-DD, got {expire_date!r}") from None

    has_second = any(v is not None for v in (second_side, second_trigger_price, second_order_price))
    if type == SINGLE and has_second:
        raise GuardrailError("invalid-order-params", "SINGLE takes no second condition")
    if type != SINGLE and (second_side is None or second_trigger_price is None):
        raise GuardrailError("invalid-order-params",
                             f"{type} needs second_side and second_trigger_price")

    order_prices = [first_order_price, second_order_price] if has_second else [first_order_price]
    if order_type == LIMIT and any(p is None for p in order_prices):
        raise GuardrailError("invalid-order-params", "LIMIT needs an order price for every condition")
    if order_type != LIMIT and any(p is not None for p in order_prices):
        raise GuardrailError("invalid-order-params", "MARKET takes no order price")

    second = _leg(second_side, second_trigger_price, second_order_price) if has_second else None
    return {"type": type, "quantity": quantity, "order_type": order_type, "expire_date": expire_date,
            "first": _leg(first_side, first_trigger_price, first_order_price), "second": second}


def _leg_price(order_type: str, leg: dict) -> Decimal | None:
    value = leg.get("orderPrice" if order_type == LIMIT else "triggerPrice")
    return None if value is None else to_decimal(value)


def group_notional(group_type: str, quantity: str, order_type: str,
                   first: dict, second: dict | None) -> Decimal | None:
    """SINGLE = its one leg, OCO = the larger leg (only one fires), OTO = both legs.
    None when a leg has no price to count (e.g. a PROFIT_RATE condition made in the app)."""
    legs = [first] if second is None else [first, second]
    prices = [_leg_price(order_type, leg) for leg in legs]
    if any(p is None for p in prices):
        return None
    amounts = [to_decimal(quantity) * p for p in prices]
    if group_type == OCO:
        return max(amounts)
    return sum(amounts, Decimal("0"))


def request_notional(request: dict) -> Decimal:
    return group_notional(request["type"], request["quantity"], request["order_type"],
                          request["first"], request["second"])


def detail_notional(detail: dict) -> Decimal | None:
    """Notional of an existing conditional order in the API's detail shape. None when it cannot
    be priced, including an unexpected shape: the caller then counts the full new notional."""
    try:
        return group_notional(detail["type"], detail["quantity"], detail["orderType"],
                              detail["first"], detail.get("second"))
    except (KeyError, TypeError, AttributeError, InvalidOperation):
        return None


def _require_live(app) -> None:
    if app.use_paper:
        raise PaperError("paper mode has no conditional orders; they are live-only")


def _audit(app, tool: str, decision: str, **fields) -> None:
    app.audit.record({"tool": tool, "mode": app.config.mode, "decision": decision,
                      "kind": CONDITIONAL_KIND, **fields})


def preview_conditional_order(app, *, symbol: str, type: str, quantity: str, order_type: str,
                              expire_date: str, first_side: str, first_trigger_price: str,
                              first_order_price: str | None = None, second_side: str | None = None,
                              second_trigger_price: str | None = None,
                              second_order_price: str | None = None,
                              confirm_high_value_order: bool = False) -> dict:
    from .tools import _price_and_currency
    _require_live(app)
    request = build_request(
        type=type, quantity=quantity, order_type=order_type, expire_date=expire_date,
        first_side=first_side, first_trigger_price=first_trigger_price,
        first_order_price=first_order_price, second_side=second_side,
        second_trigger_price=second_trigger_price, second_order_price=second_order_price,
    )
    _, currency = _price_and_currency(app, symbol)
    spec = app.safety.build_conditional_spec(
        symbol=symbol, request=request, notional=request_notional(request),
        confirm_high_value_order=confirm_high_value_order, currency=currency,
    )
    # registering is allowed any time; the broker only fires during sessions
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False)
    token = app.safety.issue_token(spec)
    _audit(app, "preview_conditional_order", "previewed", symbol=symbol, notional=spec.notional,
           currency=spec.currency, clientOrderId=spec.client_order_id, token=token)
    return {"confirmationToken": token, "clientOrderId": spec.client_order_id, "symbol": symbol,
            "type": type, "estimatedNotional": str(spec.notional), "currency": spec.currency,
            "expiresInSec": app.config.confirmation_ttl_sec, "mode": app.config.mode,
            "next": "place_order"}


def preview_conditional_modify(app, conditional_order_id: str, *, type: str, quantity: str,
                               order_type: str, expire_date: str, first_side: str,
                               first_trigger_price: str, first_order_price: str | None = None,
                               second_side: str | None = None,
                               second_trigger_price: str | None = None,
                               second_order_price: str | None = None,
                               confirm_high_value_order: bool = False) -> dict:
    from .tools import _price_and_currency
    _require_live(app)
    request = build_request(
        type=type, quantity=quantity, order_type=order_type, expire_date=expire_date,
        first_side=first_side, first_trigger_price=first_trigger_price,
        first_order_price=first_order_price, second_side=second_side,
        second_trigger_price=second_trigger_price, second_order_price=second_order_price,
    )
    original = app.client.get_conditional_order(conditional_order_id)
    symbol = original["symbol"]
    _, currency = _price_and_currency(app, symbol)
    spec = app.safety.build_conditional_spec(
        symbol=symbol, request=request, notional=request_notional(request),
        confirm_high_value_order=confirm_high_value_order, currency=currency,
        modify_id=conditional_order_id,
    )
    spec.prev_notional = detail_notional(original)  # None -> the full new notional is counted
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                                check_daily=True, prev_notional=spec.prev_notional)
    token = app.safety.issue_token(spec)
    _audit(app, "preview_conditional_modify", "modify_previewed",
           conditionalOrderId=conditional_order_id, previousStatus=original.get("status"),
           symbol=symbol, notional=spec.notional, currency=spec.currency,
           clientOrderId=spec.client_order_id, token=token)
    return {"confirmationToken": token, "conditionalOrderId": conditional_order_id,
            "symbol": symbol, "type": type, "estimatedNotional": str(spec.notional),
            "currency": spec.currency, "expiresInSec": app.config.confirmation_ttl_sec,
            "mode": app.config.mode, "next": "modify_order"}


def cancel_conditional_order(app, conditional_order_id: str) -> dict:
    """Cancelling lowers risk, so no preview; it never refunds the registration-day cap."""
    _require_live(app)
    previous = app.client.get_conditional_order(conditional_order_id)
    app.safety.check_symbol(previous["symbol"])
    app.client.cancel_conditional_order(conditional_order_id)
    _audit(app, "cancel_conditional_order", "canceled", conditionalOrderId=conditional_order_id,
           previousStatus=previous.get("status"))
    return {"conditionalOrderId": conditional_order_id, "canceled": True}


def list_conditional_orders(app, status: str = "OPEN", symbol: str | None = None,
                            cursor: str | None = None) -> dict:
    if app.use_paper:
        return {"conditionalOrders": [], "hasNext": False}
    return app.client.list_conditional_orders(status=status, symbol=symbol, cursor=cursor)


def get_conditional_order(app, conditional_order_id: str) -> dict:
    if app.use_paper:
        raise ValueError(f"paper conditional order not found: {conditional_order_id}")
    return app.client.get_conditional_order(conditional_order_id)


def execute_place(app, spec) -> dict:
    """The place_order execution step for a conditional token."""
    _require_live(app)  # a token issued live must not reach the broker from a paper instance
    return app.client.create_conditional_order(
        symbol=spec.symbol, client_order_id=spec.client_order_id,
        confirm_high_value_order=spec.confirm_high_value_order, **spec.conditional,
    )


def execute_modify(app, spec) -> dict:
    """The modify_order execution step for a conditional modify token (returns a new id)."""
    _require_live(app)
    return app.client.modify_conditional_order(
        spec.modify_order_id, confirm_high_value_order=spec.confirm_high_value_order,
        **spec.conditional,
    )
