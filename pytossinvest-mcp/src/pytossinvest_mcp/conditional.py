"""Conditional orders (SINGLE / OCO / OTO) over MCP.

Preview tools here issue confirmation tokens whose spec has kind=conditional; placing and
modifying run through tools.place_order / tools.modify_order so the safety path stays single.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pytossinvest.money import to_decimal

from .safety import GuardrailError, _is_positive_number

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
    """Notional of an existing conditional order in the API's detail shape."""
    return group_notional(detail["type"], detail["quantity"], detail["orderType"],
                          detail["first"], detail.get("second"))
