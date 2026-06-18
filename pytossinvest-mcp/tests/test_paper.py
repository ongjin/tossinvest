from decimal import Decimal

import pytest

from pytossinvest_mcp.paper import PaperBroker, PaperError


def test_starts_with_configured_cash():
    b = PaperBroker(starting_cash="1000000")
    assert b.buying_power() == Decimal("1000000")
    assert b.holdings()["items"] == []


def test_buy_fills_and_reduces_cash():
    b = PaperBroker(starting_cash="1000000")
    order = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                    fill_price="70000", quantity="10")
    assert order.status == "FILLED"
    assert order.order_id == "paper-1"
    assert b.cash == Decimal("300000")  # 1,000,000 - 70,000*10
    assert b.sellable_quantity("005930") == Decimal("10")


def test_buy_insufficient_cash_rejected():
    b = PaperBroker(starting_cash="100000")
    with pytest.raises(PaperError):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="10")


def test_buy_then_sell_realizes_pnl():
    b = PaperBroker(starting_cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="65000", quantity="10")
    b.place(symbol="005930", side="SELL", order_type="LIMIT", fill_price="70000", quantity="10")
    assert b.realized_pnl == Decimal("50000")  # (70000-65000)*10
    assert b.sellable_quantity("005930") == Decimal("0")
    assert "005930" not in b.positions


def test_sell_more_than_held_rejected():
    b = PaperBroker(starting_cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="65000", quantity="5")
    with pytest.raises(PaperError):
        b.place(symbol="005930", side="SELL", order_type="LIMIT", fill_price="70000", quantity="10")


def test_average_price_updates_on_add():
    b = PaperBroker(starting_cash="10000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="60000", quantity="10")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="80000", quantity="10")
    pos = b.positions["005930"]
    assert pos.quantity == Decimal("20")
    assert pos.avg_price == Decimal("70000")


def test_holdings_and_orders_are_strings():
    b = PaperBroker(starting_cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="70000", quantity="10",
            client_order_id="cli-1")
    h = b.holdings()
    assert h["cash"] == "300000"
    assert h["items"][0] == {"symbol": "005930", "quantity": "10", "averagePurchasePrice": "70000"}
    listed = b.list_orders()
    assert listed[0].client_order_id == "cli-1"
    assert b.get_order("paper-1").symbol == "005930"
    assert b.get_order("nope") is None
