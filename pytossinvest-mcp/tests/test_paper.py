from decimal import Decimal

import pytest

from pytossinvest_mcp.paper import PaperBroker, MemoryPaperStore, PaperError


def _broker(cash="10000000", next_id=None):
    return PaperBroker(MemoryPaperStore(starting_cash=cash), next_id=next_id)


def test_starts_with_configured_cash():
    b = _broker(cash="1000000")
    assert b.buying_power("KRW") == Decimal("1000000")
    assert b.holdings()["items"] == []


def test_scalar_starting_cash_wraps_to_krw():
    b = _broker(cash="1000000")
    assert b.holdings()["cash"] == {"KRW": "1000000"}
    assert b.holdings()["realizedPnl"] == {"KRW": "0"}


def test_buy_fills_and_reduces_cash():
    b = _broker(cash="1000000")
    order = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                    fill_price="70000", quantity="10", currency="KRW")
    assert order.status == "FILLED"
    assert order.order_id == "paper-1"
    assert b.buying_power("KRW") == Decimal("300000")  # 1,000,000 - 70,000*10
    assert b.sellable_quantity("005930") == Decimal("10")


def test_buy_insufficient_cash_rejected():
    b = _broker(cash="100000")
    with pytest.raises(PaperError, match="insufficient KRW cash"):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="10", currency="KRW")


def test_currency_buckets_are_isolated():
    # the bug this whole change fixes: a USD buy must NOT dent the KRW bucket
    b = PaperBroker(MemoryPaperStore(starting_cash={"KRW": "10000000", "USD": "7000"}))
    b.place(symbol="SOXX", side="BUY", order_type="MARKET",
            fill_price="614.87", quantity="1", currency="USD")
    h = b.holdings()
    assert h["cash"]["KRW"] == "10000000"          # untouched
    assert h["cash"]["USD"] == "6385.13"           # 7000 - 614.87
    assert b.buying_power("KRW") == Decimal("10000000")
    assert b.buying_power("USD") == Decimal("6385.13")


def test_usd_insufficient_even_when_krw_is_huge():
    b = PaperBroker(MemoryPaperStore(starting_cash={"KRW": "10000000", "USD": "500"}))
    with pytest.raises(PaperError, match="insufficient USD cash"):
        b.place(symbol="SOXX", side="BUY", order_type="MARKET",
                fill_price="614.87", quantity="1", currency="USD")


def test_buy_then_sell_realizes_pnl_per_currency():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="65000", quantity="10", currency="KRW")
    b.place(symbol="005930", side="SELL", order_type="LIMIT",
            fill_price="70000", quantity="10", currency="KRW")
    h = b.holdings()
    assert h["realizedPnl"]["KRW"] == "50000"  # (70000-65000)*10
    assert b.sellable_quantity("005930") == Decimal("0")


def test_sell_more_than_held_rejected():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="65000", quantity="5", currency="KRW")
    with pytest.raises(PaperError, match="insufficient quantity"):
        b.place(symbol="005930", side="SELL", order_type="LIMIT",
                fill_price="70000", quantity="10", currency="KRW")


def test_average_price_updates_on_add():
    b = _broker(cash="10000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="60000", quantity="10", currency="KRW")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="80000", quantity="10", currency="KRW")
    item = b.holdings()["items"][0]
    assert item["quantity"] == "20"
    assert item["averagePurchasePrice"] == "70000"
    assert item["currency"] == "KRW"


def test_holdings_and_orders_are_strings():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="10", currency="KRW", client_order_id="cli-1")
    h = b.holdings()
    assert h["cash"]["KRW"] == "300000"
    assert h["items"][0] == {"symbol": "005930", "currency": "KRW",
                             "quantity": "10", "averagePurchasePrice": "70000"}
    listed = b.list_orders()
    assert listed[0].client_order_id == "cli-1"
    assert b.get_order("paper-1").symbol == "005930"
    assert b.get_order("nope") is None


def test_place_is_idempotent_by_client_order_id():
    b = _broker(cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")
    assert o2.order_id == o1.order_id          # same order returned, not a second fill
    assert len(b.list_orders()) == 1
    assert b.holdings()["items"][0]["quantity"] == "1"  # only one fill applied
