from decimal import Decimal

import pytest

from pytossinvest.models import Account, Price, BuyingPower, OrderResponse, HoldingsItem


def test_account_parses():
    a = Account.model_validate({"accountNo": "123-45", "accountSeq": 1, "accountType": "BROKERAGE"})
    assert a.account_seq == 1
    assert a.account_type == "BROKERAGE"


def test_price_money_is_decimal():
    p = Price.model_validate({"symbol": "005930", "lastPrice": "70000", "currency": "KRW"})
    assert p.last_price == Decimal("70000")
    assert isinstance(p.last_price, Decimal)


def test_buying_power_decimal():
    bp = BuyingPower.model_validate({"currency": "KRW", "cashBuyingPower": "1000000"})
    assert bp.cash_buying_power == Decimal("1000000")


def test_order_response():
    o = OrderResponse.model_validate({"orderId": "ord-1", "clientOrderId": "cli-1"})
    assert o.order_id == "ord-1"
    assert o.client_order_id == "cli-1"


def test_holdings_item_decimal_quantity():
    item = HoldingsItem.model_validate(
        {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "currency": "KRW",
         "quantity": "10", "lastPrice": "70000", "averagePurchasePrice": "65000"}
    )
    assert item.quantity == Decimal("10")
    assert item.average_purchase_price == Decimal("65000")


def test_money_field_rejects_float():
    # The "never float" guarantee must hold at the model boundary too.
    with pytest.raises(Exception):
        Price.model_validate({"symbol": "005930", "lastPrice": 70000.5, "currency": "KRW"})
