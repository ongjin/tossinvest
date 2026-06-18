import json
from decimal import Decimal

import pytest

import pytossinvest_mcp.tools as T
from pytossinvest_mcp.safety import GuardrailError
from pytossinvest_mcp.paper import PaperError


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
    assert str(app.paper.buying_power()) == "9300000"  # 10,000,000 - 700,000
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
        T.preview_modify(app, "paper-1", order_type="LIMIT", price="71000")
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
    assert app.paper.holdings()["items"] == []
    assert str(app.paper.buying_power()) == "10000000"


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


def test_preview_modify_live_issues_token_with_merged_notional(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")  # qty 10 (orig) * 71000
    assert pv["confirmationToken"]
    assert pv["orderId"] == "real-1"
    assert pv["estimatedNotional"] == "710000"


def test_preview_then_modify_calls_client_and_releases_token(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    out = T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert out["orderId"] == "real-2"
    call = [c for c in fake_client.calls if c[0] == "modify_order"][-1]
    assert call[2]["price"] == "71000"
    with pytest.raises(GuardrailError):  # token released -> second modify fails
        T.modify_order(app, confirmation_token=pv["confirmationToken"])


def test_modify_accrues_delta_to_daily_bucket(app_factory, fake_client):
    from datetime import date
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    # original real-1: 70000 * 10 = 700,000 ; modify price -> 71000 => 710,000 ; delta +10,000
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    day = date(2026, 6, 17).isoformat()
    assert app.safety.spend_store.current(day, "KRW") == Decimal("10000")  # M1: delta accrued


def test_modify_downsize_credits_with_floor(app_factory, fake_client):
    from datetime import date
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    day = date(2026, 6, 17).isoformat()
    app.safety.spend_store.seed(day, "KRW", Decimal("700000"))  # prior bucket
    # original real-1 = 700,000 ; modify down to 60000*10 = 600,000 ; delta -100,000
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="60000")
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert app.safety.spend_store.current(day, "KRW") == Decimal("600000")  # 700,000 - 100,000 (credited)


def test_preview_modify_enforces_per_order_cap(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False,
                      max_order_amount="100000")
    with pytest.raises(GuardrailError) as e:  # 10 * 71000 = 710,000 > 100,000
        T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    assert e.value.code == "order-amount-cap"


def test_cancel_records_previous_status(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True)
    T.cancel_order(app, "real-1")
    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["decision"] == "canceled"
    assert entry["previousStatus"] == "PENDING"


def test_place_failure_releases_reservation(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, daily_order_limit="1000000",
                      enforce_market_hours=False)

    def boom(**kwargs):
        raise RuntimeError("toss 500")
    fake_client.place_order = boom

    prev = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                           quantity="1", price="100000")
    with pytest.raises(RuntimeError):
        T.place_order(app, confirmation_token=prev["confirmationToken"])
    # reservation released: a fresh full-cap-adjacent order still previews/reserves fine
    spec = app.safety.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                                 quantity="1", price="100000")
    assert app.safety.reserve(spec) is True


def test_preview_uses_authoritative_currency_from_api(app_factory, fake_client):
    app = app_factory(mode="paper")
    from pytossinvest.models import Price
    # numeric symbol that the API says is actually USD-denominated
    fake_client.get_prices = lambda symbols: [
        Price.model_validate({"symbol": symbols[0], "lastPrice": "100", "currency": "USD"})
    ]
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="1", price="100")
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    placed = [json.loads(l) for l in open(app.config.audit_log_path, encoding="utf-8")
              if json.loads(l)["decision"] == "placed"][0]
    assert placed["currency"] == "USD"  # authoritative, not symbol-shape KRW


def test_preview_falls_back_to_symbol_shape_when_price_lookup_fails(app_factory, fake_client):
    app = app_factory(mode="paper")
    def boom(symbols):
        raise RuntimeError("market data down")
    fake_client.get_prices = boom
    pv = T.preview_order(app, symbol="AAPL", side="BUY", order_type="LIMIT",
                         quantity="1", price="100")
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    placed = [json.loads(l) for l in open(app.config.audit_log_path, encoding="utf-8")
              if json.loads(l)["decision"] == "placed"][0]
    assert placed["currency"] == "USD"  # AAPL -> symbol-shape fallback


def test_market_preview_uses_single_price_call(app_factory, fake_client):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="MARKET", quantity="10")
    n = sum(1 for c in fake_client.calls if c[0] == "get_prices")
    assert n == 1  # currency + ref price share one call


def test_country_for_order_prefers_authoritative_currency():
    # authoritative currency wins over symbol shape
    assert T._country_for_order("BRK.B", "USD") == "US"   # dotted ticker (isalpha False) but USD
    assert T._country_for_order("ABCDE", "KRW") == "KR"   # alpha ticker but KRW
    assert T._country_for_order("AAPL", " usd ") == "US"  # normalized (strip/upper)
    # fall back to symbol shape when currency missing/blank
    assert T._country_for_order("AAPL", None) == "US"
    assert T._country_for_order("005930", None) == "KR"
    assert T._country_for_order("AAPL", "") == "US"


def test_market_gate_uses_authoritative_currency_for_country(app_factory, fake_client):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=True,
                      now_kst=lambda: datetime(2026, 6, 17, 10, 0, tzinfo=kst))
    # numeric symbol that the API says is USD-denominated -> US market hours
    _, enforce = T._market_gate(app, "005930", currency="USD")
    assert enforce is True
    country = [c for c in fake_client.calls if c[0] == "get_market_calendar"][-1][1]
    assert country == "US"  # authoritative USD, not symbol-shape KR


def test_preview_order_passes_authoritative_currency_to_gate(app_factory, fake_client):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from pytossinvest.models import Price
    kst = ZoneInfo("Asia/Seoul")
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=True,
                      now_kst=lambda: datetime(2026, 6, 17, 10, 0, tzinfo=kst))
    fake_client.get_prices = lambda symbols: [
        Price.model_validate({"symbol": symbols[0], "lastPrice": "100", "currency": "USD"})]
    # open for both KR and US shapes so the gate never rejects -> test asserts only the country queried
    open_cal = {"today": {"regularMarket": {"startTime": "00:00", "endTime": "23:59"},
                          "integrated": {"regularMarket": {"startTime": "00:00", "endTime": "23:59"}}}}
    def cal(country, date=None):
        fake_client.calls.append(("get_market_calendar", country))
        return open_cal
    fake_client.get_market_calendar = cal
    T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                    quantity="1", price="100")
    country = [c for c in fake_client.calls if c[0] == "get_market_calendar"][-1][1]
    assert country == "US"  # numeric symbol but API currency USD -> US hours
