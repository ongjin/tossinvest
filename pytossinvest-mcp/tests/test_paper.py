from decimal import Decimal

import pytest

from pytossinvest_mcp.paper import PaperBroker, MemoryPaperStore, PaperError


def _broker(cash="10000000", next_id=None):
    return PaperBroker(MemoryPaperStore(starting_cash=cash), next_id=next_id)


def test_starts_with_configured_cash():
    b = _broker(cash="1000000")
    assert b.buying_power() == Decimal("1000000")
    assert b.holdings()["items"] == []


def test_buy_fills_and_reduces_cash():
    b = _broker(cash="1000000")
    order = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                    fill_price="70000", quantity="10")
    assert order.status == "FILLED"
    assert order.order_id == "paper-1"
    assert b.buying_power() == Decimal("300000")  # 1,000,000 - 70,000*10
    assert b.sellable_quantity("005930") == Decimal("10")


def test_buy_insufficient_cash_rejected():
    b = _broker(cash="100000")
    with pytest.raises(PaperError):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="10")


def test_buy_then_sell_realizes_pnl():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="65000", quantity="10")
    b.place(symbol="005930", side="SELL", order_type="LIMIT", fill_price="70000", quantity="10")
    h = b.holdings()
    assert h["realizedPnl"] == "50000"  # (70000-65000)*10
    assert b.sellable_quantity("005930") == Decimal("0")


def test_sell_more_than_held_rejected():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="65000", quantity="5")
    with pytest.raises(PaperError):
        b.place(symbol="005930", side="SELL", order_type="LIMIT", fill_price="70000", quantity="10")


def test_average_price_updates_on_add():
    b = _broker(cash="10000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="60000", quantity="10")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="80000", quantity="10")
    h = b.holdings()
    item = h["items"][0]
    assert item["quantity"] == "20"
    assert item["averagePurchasePrice"] == "70000"


def test_holdings_and_orders_are_strings():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="70000", quantity="10",
            client_order_id="cli-1")
    h = b.holdings()
    assert h["cash"] == "300000"
    assert h["items"][0] == {"symbol": "005930", "quantity": "10", "averagePurchasePrice": "70000"}
    listed = b.list_orders()
    assert listed[0].client_order_id == "cli-1"
    assert b.get_order("paper-1").symbol == "005930"
    assert b.get_order("nope") is None


def test_place_is_idempotent_by_client_order_id():
    b = _broker(cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="c1")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="c1")
    assert o2.order_id == o1.order_id          # same order returned, not a second fill
    assert len(b.list_orders()) == 1
    h = b.holdings()
    assert h["items"][0]["quantity"] == "1"    # only one fill applied
