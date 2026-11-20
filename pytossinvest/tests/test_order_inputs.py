import json
from decimal import Decimal, InvalidOperation

import pytest
import respx

from pytossinvest import TossInvestClient

BASE = "https://openapi.example.test"


@pytest.mark.parametrize(("method", "kwargs", "error"), [
    ("place_order", {"symbol": "AAPL", "side": "BUY", "quantity": "1", "price": 12.34}, TypeError),
    ("place_order", {"symbol": "AAPL", "side": "BUY", "order_amount": True}, TypeError),
    ("place_order", {"symbol": "AAPL", "side": "BUY", "quantity": "0"}, ValueError),
    ("modify_order", {"order_id": "o1", "quantity": "NaN"}, InvalidOperation),
    ("modify_order", {"order_id": "o1", "price": "-1"}, ValueError),
])
def test_invalid_order_never_sends(respx_mock, method, kwargs, error):
    with TossInvestClient("cid", "secret", base_url=BASE) as client:
        with pytest.raises(error):
            getattr(client, method)(order_type="LIMIT", **kwargs)
    assert not respx_mock.calls


@pytest.mark.parametrize(("method", "target"), [
    ("create_conditional_order", {"symbol": "AAPL"}),
    ("modify_conditional_order", {"conditional_order_id": "co1"}),
])
@pytest.mark.parametrize(("field", "value", "error"), [
    ("quantity", 1.0, TypeError),
    ("first", {"orderSide": "SELL", "triggerPrice": 12.34}, TypeError),
    ("second", {"orderSide": "SELL", "triggerPrice": "10", "orderPrice": "Infinity"}, InvalidOperation),
])
def test_invalid_condition_never_sends(respx_mock, method, target, field, value, error):
    args = dict(type="OCO", quantity="1", order_type="LIMIT", expire_date="2027-01-01",
                first={"orderSide": "SELL", "triggerPrice": "20", "orderPrice": "20"},
                second={"orderSide": "SELL", "triggerPrice": "10", "orderPrice": "10"})
    args[field] = value
    with TossInvestClient("cid", "secret", base_url=BASE) as client:
        with pytest.raises(error):
            getattr(client, method)(**target, **args)
    assert not respx_mock.calls


@respx.mock
def test_decimal_order_values_on_wire():
    respx.post(f"{BASE}/oauth2/token").respond(json={
        "access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
    orders = respx.post(f"{BASE}/api/v1/orders").respond(json={"result": {"orderId": "o1"}})
    conditions = respx.post(f"{BASE}/api/v1/conditional-orders").respond(json={"result": {}})
    leg = {"orderSide": "SELL", "triggerPrice": Decimal("12.30"), "orderPrice": "12.25"}
    with TossInvestClient("cid", "secret", base_url=BASE, account_seq=1) as client:
        client.place_order(symbol="AAPL", side="BUY", order_type="LIMIT",
                           quantity=1, price=Decimal("12.30"))
        client.create_conditional_order(symbol="AAPL", type="SINGLE", quantity=1,
                                        order_type="LIMIT", expire_date="2027-01-01", first=leg)

    order = json.loads(orders.calls.last.request.content)
    assert (order["quantity"], order["price"]) == ("1", "12.30")
    condition = json.loads(conditions.calls.last.request.content)
    assert condition["quantity"] == "1"
    assert condition["first"] == {**leg, "triggerPrice": "12.30"}
    assert isinstance(leg["triggerPrice"], Decimal)
