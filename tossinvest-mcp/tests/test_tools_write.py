import json

import pytest

import tossinvest_mcp.tools as T
from tossinvest_mcp.safety import GuardrailError
from tossinvest_mcp.paper import PaperError


def test_get_order_readiness_paper(app_factory):
    app = app_factory(mode="paper")
    out = T.get_order_readiness(app, "005930")
    assert out["buyingPower"] == "10000000"
    assert out["sellableQuantity"] == "0"


def test_preview_returns_token_and_estimate(app_factory):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    assert pv["estimatedNotional"] == "700000"
    assert pv["confirmationToken"]
    assert pv["clientOrderId"]


def test_preview_rejected_by_guardrail(app_factory):
    app = app_factory(mode="paper", max_order_amount="100000")
    with pytest.raises(GuardrailError) as e:
        T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="10", price="70000")  # 700,000 > 100,000
    assert e.value.code == "order-amount-cap"


def test_place_requires_valid_token(app_factory):
    app = app_factory(mode="paper")
    with pytest.raises(GuardrailError) as e:
        T.place_order(app, confirmation_token="bogus")
    assert e.value.code == "invalid-confirmation"


def test_preview_then_place_fills_paper_and_audits(app_factory):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    out = T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert out["status"] == "FILLED"
    assert app.paper.cash.__str__() == "9300000"  # 10,000,000 - 700,000
    # token now consumed -> second place fails
    with pytest.raises(GuardrailError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    # audit log has preview + place lines
    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    tools = [json.loads(l)["tool"] for l in lines]
    assert tools == ["preview_order", "place_order"]


def test_place_live_calls_client_with_string_price(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    out = T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert out["orderId"] == "real-1"
    sent = fake_client.place_payloads[-1]
    assert sent["price"] == "70000"            # string, not number
    assert sent["client_order_id"] == pv["clientOrderId"]


def test_modify_and_cancel_are_live_only(app_factory):
    app = app_factory(mode="paper")
    with pytest.raises(PaperError):
        T.modify_order(app, "paper-1", order_type="LIMIT", price="71000")
    with pytest.raises(PaperError):
        T.cancel_order(app, "paper-1")


def test_cancel_live_calls_client(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True)
    out = T.cancel_order(app, "real-1")
    assert out["orderId"] == "real-3"
    assert ("cancel_order", "real-1") in fake_client.calls


def test_place_market_paper_no_ref_price_errors_without_corruption(app_factory, fake_client):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="MARKET", quantity="10")
    # market data momentarily unavailable at place time (it was available at preview)
    fake_client.get_prices = lambda symbols: []
    with pytest.raises(PaperError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    # no corrupt zero-price fill happened
    assert app.paper.positions == {}
    assert str(app.paper.cash) == "10000000"


def test_place_audit_records_currency_and_notional(app_factory):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    placed = [json.loads(l) for l in lines if json.loads(l)["decision"] == "placed"][0]
    assert placed["currency"] == "KRW"
    assert placed["notional"] == "700000"


def test_place_rechecks_daily_limit_after_other_fill(app_factory):
    app = app_factory(mode="paper", daily_order_limit="1000000", max_order_amount="1000000")
    pv1 = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                          quantity="10", price="70000")  # 700,000 (under limit individually)
    pv2 = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                          quantity="10", price="70000")  # 700,000 (also under, at preview time)
    T.place_order(app, confirmation_token=pv1["confirmationToken"])  # records 700,000
    with pytest.raises(GuardrailError) as e:
        T.place_order(app, confirmation_token=pv2["confirmationToken"])  # 1,400,000 > 1,000,000
    assert e.value.code == "daily-limit"
    # token NOT consumed -> still pending (idempotency preserved)
    assert app.safety.consume(pv2["confirmationToken"]).client_order_id == pv2["clientOrderId"]
