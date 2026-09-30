import json
from datetime import date
from decimal import Decimal

import pytest

import pytossinvest_mcp.conditional as C
import pytossinvest_mcp.tools as T
from pytossinvest_mcp.paper import PaperError
from pytossinvest_mcp.safety import GuardrailError

DAY = date(2026, 6, 17).isoformat()  # conftest's SafetyManager "today"
SINGLE = dict(symbol="005930", type="SINGLE", quantity="10", order_type="LIMIT",
              expire_date="2026-07-31", first_side="SELL", first_trigger_price="65000",
              first_order_price="64900")                              # 649,000 KRW
MODIFY = {k: v for k, v in SINGLE.items() if k != "symbol"}


def _live(app_factory):
    return app_factory(mode="live", allow_live=True, enforce_market_hours=False)


def _spent(app):
    return app.safety.spend_store.current(DAY, "KRW")


def _audit(app, decision):
    with open(app.config.audit_log_path, encoding="utf-8") as f:
        return [e for e in map(json.loads, f) if e["decision"] == decision]


def test_place_registers_through_the_sdk_and_reserves_the_full_notional(app_factory, fake_client):
    app = _live(app_factory)
    pv = C.preview_conditional_order(app, **SINGLE)
    assert (pv["estimatedNotional"], pv["next"]) == ("649000", "place_order")

    out = T.place_order(app, confirmation_token=pv["confirmationToken"])

    assert out["conditionalOrderId"] == "co-1"
    sent = [c[1] for c in fake_client.calls if c[0] == "create_conditional_order"][-1]
    assert sent["client_order_id"] == pv["clientOrderId"]
    assert sent["first"] == {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "64900"}
    assert fake_client.place_payloads == []          # never the ordinary order endpoint
    assert _spent(app) == Decimal("649000")
    placed = _audit(app, "placed")[-1]
    assert (placed["kind"], placed["notional"]) == ("conditional", "649000")


def test_preview_enforces_guardrails_before_issuing_a_token(app_factory):
    app = _live(app_factory)
    oto = dict(SINGLE, type="OTO", first_side="BUY", first_trigger_price="60000",
               first_order_price="60100", second_side="SELL", second_trigger_price="70000",
               second_order_price="69900")
    with pytest.raises(GuardrailError) as e:   # 601,000 + 699,000 > 1,000,000 per order
        C.preview_conditional_order(app, **oto)
    assert e.value.code == "order-amount-cap"


def test_ambiguous_failure_keeps_the_reservation(app_factory, fake_client):
    from pytossinvest.errors import ServerError
    app = _live(app_factory)

    def down(**kwargs):
        raise ServerError("internal-error", "", http_status=503)
    fake_client.create_conditional_order = down

    pv = C.preview_conditional_order(app, **SINGLE)
    with pytest.raises(ServerError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("649000")


def test_rejection_releases_the_reservation(app_factory, fake_client):
    from pytossinvest.errors import BusinessRuleError
    app = _live(app_factory)

    def reject(**kwargs):
        raise BusinessRuleError("condition-already-met", "", http_status=422)
    fake_client.create_conditional_order = reject

    pv = C.preview_conditional_order(app, **SINGLE)
    with pytest.raises(BusinessRuleError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("0")


def test_modify_reserves_only_the_delta(app_factory, fake_client):
    app = _live(app_factory)
    # original co-1: 10 x 64,900 = 649,000 ; new: 10 x 70,000 = 700,000 -> delta 51,000
    pv = C.preview_conditional_modify(app, "co-1", **dict(MODIFY, first_trigger_price="70100",
                                                          first_order_price="70000"))
    assert pv["next"] == "modify_order"

    out = T.modify_order(app, confirmation_token=pv["confirmationToken"])

    assert out["conditionalOrderId"] == "co-2"
    call = [c for c in fake_client.calls if c[0] == "modify_conditional_order"][-1]
    assert call[1] == "co-1" and call[2]["first"]["orderPrice"] == "70000"
    assert _spent(app) == Decimal("51000")
    assert _audit(app, "modified")[-1]["kind"] == "conditional"


def test_modify_counts_the_full_notional_when_the_original_cannot_be_priced(app_factory, fake_client):
    app = _live(app_factory)
    fake_client.get_conditional_order = lambda cid: {
        "conditionalOrderId": cid, "type": "SINGLE", "status": "WATCHING", "symbol": "005930",
        "quantity": "10", "orderType": "MARKET",
        "first": {"type": "PROFIT_RATE", "status": "WATCHING", "triggerPrice": None,
                  "targetProfitRate": "10"}}
    pv = C.preview_conditional_modify(app, "co-1", **MODIFY)
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("649000")


def test_cancel_is_audited_and_refunds_nothing(app_factory, fake_client):
    app = _live(app_factory)
    pv = C.preview_conditional_order(app, **SINGLE)
    T.place_order(app, confirmation_token=pv["confirmationToken"])

    out = C.cancel_conditional_order(app, "co-1")

    assert out == {"conditionalOrderId": "co-1", "canceled": True}
    assert ("cancel_conditional_order", "co-1") in fake_client.calls
    assert _spent(app) == Decimal("649000")
    assert _audit(app, "canceled")[-1]["previousStatus"] == "WATCHING"


def test_paper_writes_are_live_only_and_reads_are_empty(app_factory):
    app = app_factory(mode="paper")
    with pytest.raises(PaperError):
        C.preview_conditional_order(app, **SINGLE)
    with pytest.raises(PaperError):
        C.preview_conditional_modify(app, "co-1", **MODIFY)
    with pytest.raises(PaperError):
        C.cancel_conditional_order(app, "co-1")
    assert C.list_conditional_orders(app) == {"conditionalOrders": [], "hasNext": False}
    with pytest.raises(ValueError):
        C.get_conditional_order(app, "co-1")


def test_reads_use_the_real_account_outside_paper(app_factory, fake_client):
    app = app_factory(mode="read_only")
    C.list_conditional_orders(app, "CLOSED", cursor="c-2")
    assert ("list_conditional_orders", "CLOSED", None, "c-2") in fake_client.calls
    assert C.get_conditional_order(app, "co-1")["status"] == "WATCHING"


def test_a_conditional_modify_token_cannot_register_a_new_order(app_factory, fake_client):
    app = _live(app_factory)
    pv = C.preview_conditional_modify(app, "co-1", **MODIFY)   # delta 0 -> reserves nothing
    with pytest.raises(GuardrailError) as e:
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert e.value.code == "wrong-token"
    assert not [c for c in fake_client.calls if c[0] == "create_conditional_order"]


def test_shrinking_a_conditional_order_refunds_nothing(app_factory, fake_client):
    # it may have been counted on an earlier day; a shrink-then-cancel would otherwise be a refund
    app = _live(app_factory)
    app.safety.spend_store.seed(DAY, "KRW", Decimal("900000"))
    pv = C.preview_conditional_modify(app, "co-1", **dict(MODIFY, quantity="1"))  # 649,000 -> 64,900
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("900000")
    assert _audit(app, "modified")[-1]["notional"] == "0"


def test_a_paper_instance_refuses_a_conditional_token(app_factory, fake_client):
    # e.g. a live and a paper instance behind one redis, or a live->paper restart within the TTL
    live = _live(app_factory)
    pv = C.preview_conditional_order(live, **SINGLE)
    paper = app_factory(mode="paper")
    paper.safety = live.safety
    with pytest.raises(PaperError):
        T.place_order(paper, confirmation_token=pv["confirmationToken"])
    assert not [c for c in fake_client.calls if c[0] == "create_conditional_order"]


def test_duplicate_on_retry_keeps_the_reservation(app_factory, fake_client):
    # after a timed-out first attempt that did register, a same-token retry can meet the
    # one-OCO/OTO-per-symbol rule instead of the idempotent echo
    from pytossinvest.errors import BusinessRuleError
    app = _live(app_factory)

    def duplicate(**kwargs):
        raise BusinessRuleError("duplicate-conditional-order", "", http_status=422)
    fake_client.create_conditional_order = duplicate

    pv = C.preview_conditional_order(app, **SINGLE)
    with pytest.raises(BusinessRuleError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("649000")


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_place_then_modify_through_either_state_backend(app_factory, fake_client, backend):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False, backend=backend)
    pv = C.preview_conditional_order(app, **SINGLE)
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    pm = C.preview_conditional_modify(app, "co-1", **dict(MODIFY, first_trigger_price="70100",
                                                          first_order_price="70000"))
    T.modify_order(app, confirmation_token=pm["confirmationToken"])
    assert _spent(app) == Decimal("700000")   # 649,000 on registration + 51,000 modify delta
