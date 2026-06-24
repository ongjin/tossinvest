from decimal import Decimal

import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.paper import PaperBroker, Position, PaperError
from pytossinvest_mcp.redis_stores import (
    RedisPaperStore, _paper_state_to_dict, _paper_state_from_dict,
)


@pytest.fixture
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _broker(r, cash="1000000"):
    return PaperBroker(RedisPaperStore(r, starting_cash=cash))


def test_buy_then_holdings_decimal_exact(r):
    b = _broker(r, cash="1000")
    b.place(symbol="AAPL", side="BUY", order_type="LIMIT",
            fill_price="0.1", quantity="3", currency="KRW", client_order_id="c1")
    h = b.holdings()
    assert h["cash"]["KRW"] == "999.7"                # 1000 - 0.3, exact (not 999.6999...)
    assert h["items"][0]["averagePurchasePrice"] == "0.1"
    assert h["items"][0]["quantity"] == "3"


def test_sell_realizes_pnl(r):
    b = _broker(r, cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="2", currency="KRW", client_order_id="c1")
    b.place(symbol="005930", side="SELL", order_type="LIMIT",
            fill_price="80000", quantity="1", currency="KRW", client_order_id="c2")
    h = b.holdings()
    assert h["realizedPnl"]["KRW"] == "10000"         # (80000-70000)*1
    assert h["items"][0]["quantity"] == "1"


def test_insufficient_cash_raises(r):
    b = _broker(r, cash="100")
    with pytest.raises(PaperError, match="insufficient KRW cash"):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")


def test_usd_bucket_isolated_over_redis(r):
    b = PaperBroker(RedisPaperStore(r, starting_cash={"KRW": "10000000", "USD": "7000"}))
    b.place(symbol="SOXX", side="BUY", order_type="MARKET",
            fill_price="614.87", quantity="1", currency="USD", client_order_id="c1")
    h = b.holdings()
    assert h["cash"]["KRW"] == "10000000"
    assert h["cash"]["USD"] == "6385.13"
    assert h["items"][0]["currency"] == "USD"


def test_place_idempotent_by_client_order_id(r):
    b = _broker(r, cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="dup")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="dup")
    assert o2.order_id == o1.order_id
    assert len(b.list_orders()) == 1


def test_two_brokers_share_state(r):
    a = _broker(r, cash="1000000")
    b = PaperBroker(RedisPaperStore(r, starting_cash="1000000"))  # same fakeredis
    a.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")
    # instance b sees instance a's fill
    assert b.sellable_quantity("005930") == Decimal("1")
    assert b.holdings()["items"][0]["quantity"] == "1"


def test_concurrent_same_coid_single_fill(r):
    a = _broker(r, cash="1000000")
    b = PaperBroker(RedisPaperStore(r, starting_cash="1000000"))
    o1 = a.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="same")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="same")
    assert o1.order_id == o2.order_id           # dedup across instances
    assert len(a.list_orders()) == 1            # one fill total


def test_legacy_scalar_state_migrates_on_load(r):
    # an old redis paper key (single scalar cash, no per-position currency) must load, not crash
    legacy = {
        "cash": "500000",
        "realized_pnl": "0",
        "counter": 1,
        "positions": {"005930": {"quantity": "2", "avg_price": "70000"}},
        "orders": [],
    }
    import json
    r.set("paper", json.dumps(legacy))
    state = _paper_state_from_dict(json.loads(r.get("paper")))
    assert state.cash == {"KRW": Decimal("500000")}
    assert state.realized_pnl == {"KRW": Decimal("0")}
    assert state.positions["005930"].currency == "KRW"  # numeric symbol -> KRW


def test_round_trip_serialization(r):
    state = _make_two_currency_state()
    d = _paper_state_to_dict(state)
    back = _paper_state_from_dict(d)
    assert back.cash == {"KRW": Decimal("100"), "USD": Decimal("50")}
    assert back.positions["SOXX"].currency == "USD"


def _make_two_currency_state():
    from pytossinvest_mcp.paper import PaperState
    return PaperState(
        cash={"KRW": Decimal("100"), "USD": Decimal("50")},
        positions={"SOXX": Position(quantity=Decimal("1"), avg_price=Decimal("10"), currency="USD")},
        orders=[],
        realized_pnl={"KRW": Decimal("0"), "USD": Decimal("0")},
        counter=0,
    )
