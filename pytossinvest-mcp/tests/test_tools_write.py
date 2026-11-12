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
    assert str(app.paper.buying_power("KRW")) == "9300000"  # 10,000,000 - 700,000
    # token now consumed -> second place fails
    with pytest.raises(GuardrailError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    # audit log: the preview, the reservation written before the fill, then the fill
    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    logged = [(json.loads(l)["tool"], json.loads(l)["decision"]) for l in lines]
    assert logged == [("preview_order", "previewed"), ("place_order", "reserved"),
                      ("place_order", "placed")]


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
    assert str(app.paper.buying_power("KRW")) == "10000000"


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


def test_modify_downsize_refunds_nothing(app_factory, fake_client):
    # like a cancel: making an order smaller does not hand today's budget back
    from datetime import date
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    day = date(2026, 6, 17).isoformat()
    app.safety.spend_store.seed(day, "KRW", Decimal("700000"))  # prior bucket
    # original real-1 = 700,000 ; modify down to 60000*10 = 600,000
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="60000")
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert app.safety.spend_store.current(day, "KRW") == Decimal("700000")


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


def _live_cap_app(app_factory):
    # per-order 1,000,000 (default) / daily 1,000,000: two 600,000 orders cannot both fit
    return app_factory(mode="live", allow_live=True, daily_order_limit="1000000",
                       enforce_market_hours=False)


def _fresh_600k_fits(app):
    spec = app.safety.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                                 quantity="6", price="100000")
    return app.safety.reserve(spec)


def test_place_rejected_by_toss_releases_reservation(app_factory, fake_client):
    from pytossinvest.errors import BusinessRuleError
    app = _live_cap_app(app_factory)

    def reject(**kwargs):
        raise BusinessRuleError("insufficient-buying-power", "", http_status=422)
    fake_client.place_order = reject

    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="6", price="100000")
    with pytest.raises(BusinessRuleError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert _fresh_600k_fits(app) is True  # a definite 4xx rejection frees the budget


def test_place_ambiguous_failure_keeps_reservation(app_factory, fake_client):
    from pytossinvest.errors import ServerError
    app = _live_cap_app(app_factory)
    real_place = fake_client.place_order

    def timeout(**kwargs):
        raise ServerError("internal-error", "", http_status=503)  # may or may not have filled
    fake_client.place_order = timeout

    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="6", price="100000")
    with pytest.raises(ServerError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    # a second order via a NEW preview must not slip past the cap
    assert _fresh_600k_fits(app) is False

    # retrying the same token is still allowed and counted once
    fake_client.place_order = real_place
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    from datetime import date
    assert app.safety.spend_store.current(date(2026, 6, 17).isoformat(), "KRW") == Decimal("600000")


def test_modify_ambiguous_failure_keeps_reservation(app_factory, fake_client):
    from datetime import date
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)

    def boom(order_id, **kwargs):
        raise RuntimeError("connection reset")  # unknown outcome
    fake_client.modify_order = boom

    # original real-1 = 700,000 ; modify to 71000*10 = 710,000 ; delta +10,000
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    with pytest.raises(RuntimeError):
        T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert app.safety.spend_store.current(date(2026, 6, 17).isoformat(), "KRW") == Decimal("10000")


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
    assert T._country_for_order("BRK.B", None) == "US"  # punctuated US ticker, no quote


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


def test_a_modify_token_cannot_place_a_new_order(app_factory, fake_client):
    # it reserves only the modify delta, so placing with it would dodge the daily cap
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    with pytest.raises(GuardrailError) as e:
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert e.value.code == "wrong-token"
    assert fake_client.place_payloads == []
    T.modify_order(app, confirmation_token=pv["confirmationToken"])  # still valid where it belongs


def test_a_place_token_cannot_modify(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="1", price="70000")
    with pytest.raises(GuardrailError) as e:
        T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert e.value.code == "wrong-token"


def test_cancel_order_respects_the_deny_list(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, deny_symbols=["005930"])
    with pytest.raises(GuardrailError) as e:
        T.cancel_order(app, "real-1")   # real-1 is a 005930 order
    assert e.value.code == "symbol-denied"
    assert not [c for c in fake_client.calls if c[0] == "cancel_order"]


def _place_600k_failing_with(app, fake_client, *errors):
    """Preview one 600,000 order, then place it once per error with the same token."""
    attempts = iter(errors)

    def fail(**kwargs):
        raise next(attempts)
    fake_client.place_order = fail

    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="6", price="100000")
    for error in errors:
        with pytest.raises(type(error)):
            T.place_order(app, confirmation_token=pv["confirmationToken"])


def test_request_in_progress_keeps_reservation(app_factory, fake_client):
    # 409 request-in-progress: the same clientOrderId is still being processed and may fill
    from pytossinvest.errors import ConflictError
    app = _live_cap_app(app_factory)

    _place_600k_failing_with(
        app, fake_client, ConflictError("request-in-progress", "", http_status=409))

    assert _fresh_600k_fits(app) is False


def test_refusal_of_a_retry_keeps_a_reservation_whose_first_attempt_is_unknown(
        app_factory, fake_client):
    # the first attempt may have filled; a 401 on the retry never reached order handling,
    # so it proves nothing about the first
    from pytossinvest.errors import AuthError, ServerError
    app = _live_cap_app(app_factory)

    _place_600k_failing_with(
        app, fake_client,
        ServerError("internal-error", "", http_status=503),
        AuthError("token-revoked", "", http_status=401),
    )

    assert _fresh_600k_fits(app) is False


def test_refusal_of_a_first_attempt_still_releases(app_factory, fake_client):
    from pytossinvest.errors import AuthError
    app = _live_cap_app(app_factory)

    _place_600k_failing_with(app, fake_client, AuthError("token-revoked", "", http_status=401))

    assert _fresh_600k_fits(app) is True


def test_modify_refusal_after_an_unknown_outcome_keeps_reservation(app_factory, fake_client):
    # the first modify may have applied (+10,000) before the order filled; "already-filled"
    # on the retry does not undo it
    from datetime import date
    from pytossinvest.errors import ConflictError
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    attempts = iter([RuntimeError("connection reset"),
                     ConflictError("already-filled", "", http_status=409)])

    def fail(order_id, **kwargs):
        raise next(attempts)
    fake_client.modify_order = fail

    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    with pytest.raises(RuntimeError):
        T.modify_order(app, confirmation_token=pv["confirmationToken"])
    with pytest.raises(ConflictError):
        T.modify_order(app, confirmation_token=pv["confirmationToken"])

    assert app.safety.spend_store.current(date(2026, 6, 17).isoformat(), "KRW") == Decimal("10000")


def test_downsizing_an_order_never_counted_here_frees_no_budget(app_factory, fake_client):
    # An order placed in the app was never counted by this server. If shrinking it through
    # the MCP lowered today's total, new orders could go out past the cap.
    app = _live_cap_app(app_factory)  # daily cap 1,000,000
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="6", price="100000")
    T.place_order(app, confirmation_token=pv["confirmationToken"])  # counted: 600,000

    # the app order: 70000 x 10 = 700,000 -> 10000 x 10 = 100,000
    pm = T.preview_modify(app, "app-order", order_type="LIMIT", price="10000")
    T.modify_order(app, confirmation_token=pm["confirmationToken"])

    assert _fresh_600k_fits(app) is False  # 600,000 + 600,000 > 1,000,000


def test_unreadable_success_response_keeps_reservation(app_factory, fake_client):
    # the SDK raises for a 200 it cannot read, but a 200 means the broker took the order
    from pytossinvest.errors import TossInvestError
    app = _live_cap_app(app_factory)

    _place_600k_failing_with(
        app, fake_client, TossInvestError("missing-result", "", http_status=200))

    assert _fresh_600k_fits(app) is False


def _audit_failing_on(app, decision):
    """Make the audit log fail for one kind of event, as a full disk or a redis blip would."""
    record = app.audit.record

    def failing(event):
        if event.get("decision") == decision:
            raise OSError("No space left on device")
        record(event)
    app.audit.record = failing


def _preview_600k(app):
    return T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                           quantity="6", price="100000")["confirmationToken"]


def test_order_is_not_sent_when_its_reservation_cannot_be_logged(app_factory, fake_client):
    # the log line written before sending is what a restart restores the reservation from
    app = _live_cap_app(app_factory)
    token = _preview_600k(app)
    _audit_failing_on(app, "reserved")

    with pytest.raises(GuardrailError, match="state-unavailable"):
        T.place_order(app, confirmation_token=token)

    assert fake_client.place_payloads == []
    assert _fresh_600k_fits(app) is True  # nothing was sent, so nothing stays counted


def test_a_placed_order_is_returned_even_if_recording_it_fails(app_factory, fake_client):
    # raising here would tell the caller the order failed; its natural answer is a new
    # preview and a second real order
    app = _live_cap_app(app_factory)
    token = _preview_600k(app)
    _audit_failing_on(app, "placed")

    result = T.place_order(app, confirmation_token=token)

    assert result["orderId"] == "real-1"
    assert "No space left on device" in result["warning"]
    assert _fresh_600k_fits(app) is False  # still counted


def test_a_placed_order_is_returned_even_if_the_token_cannot_be_cleared(app_factory, fake_client):
    app = _live_cap_app(app_factory)
    token = _preview_600k(app)

    def unreachable(token):
        raise ConnectionError("store is down")
    app.safety.token_store.delete = unreachable

    result = T.place_order(app, confirmation_token=token)

    assert result["orderId"] == "real-1"
    assert "warning" in result
    assert len(fake_client.place_payloads) == 1


def test_a_modified_order_is_returned_even_if_recording_it_fails(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    _audit_failing_on(app, "modified")

    result = T.modify_order(app, confirmation_token=pv["confirmationToken"])

    assert result["orderId"] == "real-2"
    assert "warning" in result


def test_the_broker_error_is_what_surfaces_when_logging_it_fails(app_factory, fake_client):
    from pytossinvest.errors import BusinessRuleError
    app = _live_cap_app(app_factory)
    token = _preview_600k(app)
    _audit_failing_on(app, "error")

    def reject(**kwargs):
        raise BusinessRuleError("insufficient-buying-power", "", http_status=422)
    fake_client.place_order = reject

    with pytest.raises(BusinessRuleError):
        T.place_order(app, confirmation_token=token)


class _ProcessDied(BaseException):
    """Stands in for the server being killed while a call is in flight."""


@pytest.mark.parametrize("outcome", ["unknown", "died"])
def test_a_restart_restores_a_reservation_that_never_settled(app_factory, fake_client, outcome):
    from datetime import datetime, timezone
    from pytossinvest.errors import ServerError
    from pytossinvest_mcp.audit import AuditLog

    def app_on_the_same_audit_log():
        app = _live_cap_app(app_factory)
        app.audit = AuditLog(app.config.audit_log_path,
                             now=lambda: datetime(2026, 6, 17, 1, 0, tzinfo=timezone.utc))
        return app

    app = app_on_the_same_audit_log()
    token = _preview_600k(app)

    def fail(**kwargs):
        if outcome == "died":
            raise _ProcessDied()
        raise ServerError("internal-error", "", http_status=503)  # may have filled
    fake_client.place_order = fail
    with pytest.raises((ServerError, _ProcessDied)):
        T.place_order(app, confirmation_token=token)

    restarted = app_on_the_same_audit_log()  # memory stores start empty
    restarted.safety.restore_spend(restarted.audit.read_events())

    assert _fresh_600k_fits(restarted) is False


def test_paper_retry_that_fails_keeps_the_reservation_of_a_fill_that_happened(app_factory, fake_client):
    # the fill went through but the token could not be cleared, so the same token is retried;
    # a failure of that retry says nothing about the fill
    app = app_factory(mode="paper", daily_order_limit="1000000")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="MARKET", quantity="6")
    delete = app.safety.token_store.delete

    def unreachable(token):
        raise ConnectionError("store is down")
    app.safety.token_store.delete = unreachable
    first = T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert first["status"] == "FILLED" and "warning" in first
    app.safety.token_store.delete = delete

    fake_client.get_prices = lambda symbols: []  # no reference price for the MARKET retry
    with pytest.raises(PaperError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])

    from datetime import date
    assert app.safety.spend_store.current(date(2026, 6, 17).isoformat(), "KRW") == Decimal("420000")


def test_paper_fill_interrupted_by_a_store_failure_keeps_its_reservation(app_factory):
    # with the paper state in redis the fill may have been saved before the failure
    app = app_factory(mode="paper", daily_order_limit="1000000")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="6", price="100000")

    def saved_then_lost_connection(**kwargs):
        raise ConnectionError("redis went away after the write")
    app.paper.place = saved_then_lost_connection

    with pytest.raises(ConnectionError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])

    assert _fresh_600k_fits(app) is False


def test_the_broker_error_surfaces_when_the_reservation_cannot_be_released(app_factory, fake_client):
    from pytossinvest.errors import BusinessRuleError
    app = _live_cap_app(app_factory)
    token = _preview_600k(app)

    def reject(**kwargs):
        raise BusinessRuleError("insufficient-buying-power", "", http_status=422)
    fake_client.place_order = reject

    def unreachable(*args, **kwargs):
        raise ConnectionError("store is down")
    app.safety.spend_store.release = unreachable

    with pytest.raises(BusinessRuleError):
        T.place_order(app, confirmation_token=token)

    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    last = json.loads(lines[-1])
    assert (last["decision"], last["reservation"]) == ("error", "kept")


def test_retry_whose_reservation_cannot_be_logged_keeps_the_earlier_reservation(
        app_factory, fake_client):
    # the first attempt may have filled; failing to log the retry must not release it
    from pytossinvest.errors import ServerError
    app = _live_cap_app(app_factory)
    token = _preview_600k(app)

    def unknown(**kwargs):
        raise ServerError("internal-error", "", http_status=503)
    fake_client.place_order = unknown
    with pytest.raises(ServerError):
        T.place_order(app, confirmation_token=token)

    _audit_failing_on(app, "reserved")
    with pytest.raises(GuardrailError, match="state-unavailable"):
        T.place_order(app, confirmation_token=token)

    assert _fresh_600k_fits(app) is False
