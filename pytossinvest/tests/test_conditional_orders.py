import json

import httpx
import pytest
import respx

from pytossinvest.client import TossInvestClient

BASE = "https://openapi.example.test"
CO = f"{BASE}/api/v1/conditional-orders"
STOP = {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "64900"}
PROFIT = {"orderSide": "SELL", "triggerPrice": "80000", "orderPrice": "79900"}


@pytest.fixture
def client():
    respx.post(f"{BASE}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}))
    respx.get(f"{BASE}/api/v1/accounts").mock(return_value=httpx.Response(
        200, json={"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]}))
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)


@respx.mock
def test_create_sends_the_wire_payload(client):
    route = respx.post(CO).mock(return_value=httpx.Response(
        200, json={"result": {"conditionalOrderId": "co-1", "clientOrderId": "cid-1"}}))

    out = client.create_conditional_order(
        symbol="005930", type="OCO", quantity="10", order_type="LIMIT",
        expire_date="2026-10-31", first=PROFIT, second=STOP, client_order_id="cid-1",
    )

    assert out == {"conditionalOrderId": "co-1", "clientOrderId": "cid-1"}
    req = route.calls[0].request
    assert req.headers["X-Tossinvest-Account"] == "7"
    assert json.loads(req.content) == {
        "symbol": "005930", "type": "OCO", "quantity": "10", "orderType": "LIMIT",
        "expireDate": "2026-10-31", "first": PROFIT, "second": STOP,
        "clientOrderId": "cid-1", "confirmHighValueOrder": False,
    }


@respx.mock
def test_create_single_omits_second_and_client_order_id(client):
    route = respx.post(CO).mock(return_value=httpx.Response(
        200, json={"result": {"conditionalOrderId": "co-2"}}))

    client.create_conditional_order(symbol="005930", type="SINGLE", quantity="1",
                                    order_type="LIMIT", expire_date="2026-10-31", first=STOP)

    body = json.loads(route.calls[0].request.content)
    assert "second" not in body and "clientOrderId" not in body


@respx.mock
def test_modify_resets_the_whole_order_without_symbol(client):
    route = respx.post(f"{CO}/co-1/modify").mock(return_value=httpx.Response(
        200, json={"result": {"conditionalOrderId": "co-9"}}))

    out = client.modify_conditional_order("co-1", type="SINGLE", quantity="5", order_type="LIMIT",
                                          expire_date="2026-11-30", first=STOP)

    assert out == {"conditionalOrderId": "co-9"}  # modify re-creates under a new id
    assert json.loads(route.calls[0].request.content) == {
        "type": "SINGLE", "quantity": "5", "orderType": "LIMIT", "expireDate": "2026-11-30",
        "first": STOP, "confirmHighValueOrder": False,
    }


@respx.mock
def test_cancel_accepts_204_without_a_body(client):
    route = respx.delete(f"{CO}/co-1").mock(return_value=httpx.Response(204))

    assert client.cancel_conditional_order("co-1") is None
    assert route.called


@respx.mock
def test_list_and_get(client):
    listed = respx.get(CO).mock(return_value=httpx.Response(
        200, json={"result": {"conditionalOrders": [], "nextCursor": None, "hasNext": False}}))
    detail = respx.get(f"{CO}/co-1").mock(return_value=httpx.Response(
        200, json={"result": {"conditionalOrderId": "co-1", "status": "WATCHING"}}))

    assert client.list_conditional_orders("CLOSED", cursor="c-2")["hasNext"] is False
    assert dict(listed.calls[0].request.url.params) == {"status": "CLOSED", "limit": "20", "cursor": "c-2"}
    assert client.get_conditional_order("co-1")["status"] == "WATCHING"
    assert detail.called
