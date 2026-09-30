from decimal import Decimal

import pytest

from pytossinvest_mcp.conditional import build_request, detail_notional, request_notional
from pytossinvest_mcp.safety import GuardrailError

BASE = dict(type="SINGLE", quantity="10", order_type="LIMIT", expire_date="2026-07-31",
            first_side="SELL", first_trigger_price="65000", first_order_price="64900")
OCO = dict(BASE, type="OCO", first_trigger_price="80000", first_order_price="79900",
           second_side="SELL", second_trigger_price="65000", second_order_price="64900")
OTO = dict(BASE, type="OTO", first_side="BUY", first_trigger_price="60000", first_order_price="60100",
           second_side="SELL", second_trigger_price="70000", second_order_price="69900")


def test_build_request_returns_sdk_kwargs():
    assert build_request(**BASE) == {
        "type": "SINGLE", "quantity": "10", "order_type": "LIMIT", "expire_date": "2026-07-31",
        "first": {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "64900"},
        "second": None,
    }


def test_market_legs_carry_no_order_price():
    req = build_request(**dict(BASE, order_type="MARKET", first_order_price=None))
    assert req["first"] == {"orderSide": "SELL", "triggerPrice": "65000"}


@pytest.mark.parametrize("kw, expected", [
    (BASE, "649000"),                                                     # SINGLE LIMIT: 10 x orderPrice
    (dict(BASE, order_type="MARKET", first_order_price=None), "650000"),  # MARKET: 10 x triggerPrice
    (OCO, "799000"),                                                      # OCO: the larger leg
    (OTO, "1300000"),                                                     # OTO: both legs
])
def test_request_notional(kw, expected):
    assert request_notional(build_request(**kw)) == Decimal(expected)


@pytest.mark.parametrize("kw, code", [
    (dict(BASE, quantity="ten"), "invalid-order-value"),
    (dict(BASE, first_trigger_price="NaN"), "invalid-order-value"),
    (dict(BASE, first_order_price="0"), "invalid-order-value"),
    (dict(BASE, expire_date="31/07/2026"), "invalid-order-params"),
    (dict(BASE, second_side="SELL", second_trigger_price="60000", second_order_price="59900"),
     "invalid-order-params"),                                       # SINGLE with a second leg
    (dict(BASE, type="OCO"), "invalid-order-params"),               # OCO without a second leg
    (dict(BASE, first_order_price=None), "invalid-order-params"),   # LIMIT without order price
    (dict(BASE, order_type="MARKET"), "invalid-order-params"),      # MARKET with an order price
])
def test_build_request_rejects_readably(kw, code):
    with pytest.raises(GuardrailError) as e:
        build_request(**kw)
    assert e.value.code == code


def test_detail_notional_prices_the_api_shape():
    detail = {"type": "SINGLE", "quantity": "10", "orderType": "LIMIT",
              "first": {"type": "STOP", "status": "WATCHING", "triggerPrice": "65000",
                        "orderPrice": "64900"}}
    assert detail_notional(detail) == Decimal("649000")


def test_detail_notional_is_unknown_without_prices():
    detail = {"type": "SINGLE", "quantity": "10", "orderType": "MARKET",
              "first": {"type": "PROFIT_RATE", "status": "WATCHING", "triggerPrice": None,
                        "targetProfitRate": "10"}}
    assert detail_notional(detail) is None
