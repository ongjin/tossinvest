import httpx
import respx

from pytossinvest.client import TossInvestClient
from pytossinvest.models import Account, Price, BuyingPower, OrderResponse
from decimal import Decimal

BASE = "https://openapi.tossinvest.com"


def _token():
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
    )


def _client():
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)


@respx.mock
def test_get_accounts_caches_seq():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    c = _client()
    accounts = c.get_accounts()
    assert isinstance(accounts[0], Account)
    assert c._account_seq == 9  # auto-cached from first account


@respx.mock
def test_get_prices_returns_models():
    _token()
    route = respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(200, json={"result": [{"symbol": "005930", "lastPrice": "70000", "currency": "KRW"}]})
    )
    c = _client()
    prices = c.get_prices(["005930", "000660"])
    assert prices[0].last_price == Decimal("70000")
    assert route.calls.last.request.url.params["symbols"] == "005930,000660"


@respx.mock
def test_get_buying_power():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    respx.get(f"{BASE}/api/v1/buying-power").mock(
        return_value=httpx.Response(200, json={"result": {"currency": "KRW", "cashBuyingPower": "500000"}})
    )
    c = _client()
    c.get_accounts()
    bp = c.get_buying_power("KRW")
    assert isinstance(bp, BuyingPower)
    assert bp.cash_buying_power == Decimal("500000")


@respx.mock
def test_place_order_sends_idempotency_key():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    route = respx.post(f"{BASE}/api/v1/orders").mock(
        return_value=httpx.Response(200, json={"result": {"orderId": "ord-1", "clientOrderId": "cli-1"}})
    )
    c = _client()
    c.get_accounts()
    resp = c.place_order(symbol="005930", side="BUY", order_type="LIMIT",
                         price="70000", quantity="10", client_order_id="cli-1")
    assert isinstance(resp, OrderResponse)
    assert resp.order_id == "ord-1"
    sent = route.calls.last.request
    import json as _json
    payload = _json.loads(sent.content)
    assert payload["clientOrderId"] == "cli-1"
    assert payload["price"] == "70000"  # string, not number


@respx.mock
def test_cancel_order():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    respx.post(f"{BASE}/api/v1/orders/ord-1/cancel").mock(
        return_value=httpx.Response(200, json={"result": {"orderId": "ord-2"}})
    )
    c = _client()
    c.get_accounts()
    out = c.cancel_order("ord-1")
    assert out["orderId"] == "ord-2"


@respx.mock
def test_get_holdings_raw():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json={"result": {"items": [], "marketValue": {"krw": "0"}}})
    )
    c = _client()
    c.get_accounts()
    h = c.get_holdings()
    assert h["items"] == []
