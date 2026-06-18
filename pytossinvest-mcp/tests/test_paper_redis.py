from decimal import Decimal

import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.paper import PaperBroker, PaperError
from pytossinvest_mcp.redis_stores import RedisPaperStore


@pytest.fixture
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _broker(r, cash="1000000"):
    return PaperBroker(RedisPaperStore(r, starting_cash=cash))


def test_buy_then_holdings_decimal_exact(r):
    b = _broker(r, cash="1000")
    b.place(symbol="AAPL", side="BUY", order_type="LIMIT",
            fill_price="0.1", quantity="3", client_order_id="c1")
    h = b.holdings()
    assert h["cash"] == "999.7"                       # 1000 - 0.3, exact (not 999.6999...)
    assert h["items"][0]["averagePurchasePrice"] == "0.1"
    assert h["items"][0]["quantity"] == "3"


def test_sell_realizes_pnl(r):
    b = _broker(r, cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="2", client_order_id="c1")
    b.place(symbol="005930", side="SELL", order_type="LIMIT",
            fill_price="80000", quantity="1", client_order_id="c2")
    h = b.holdings()
    assert h["realizedPnl"] == "10000"               # (80000-70000)*1
    assert h["items"][0]["quantity"] == "1"


def test_insufficient_cash_raises(r):
    b = _broker(r, cash="100")
    with pytest.raises(PaperError, match="insufficient cash"):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="1", client_order_id="c1")


def test_place_idempotent_by_client_order_id(r):
    b = _broker(r, cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="dup")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="dup")
    assert o2.order_id == o1.order_id
    assert len(b.list_orders()) == 1


def test_two_brokers_share_state(r):
    a = _broker(r, cash="1000000")
    b = PaperBroker(RedisPaperStore(r, starting_cash="1000000"))  # same fakeredis
    a.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="1", client_order_id="c1")
    # instance b sees instance a's fill
    assert b.sellable_quantity("005930") == Decimal("1")
    assert b.holdings()["items"][0]["quantity"] == "1"
