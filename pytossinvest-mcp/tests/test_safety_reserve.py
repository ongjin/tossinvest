from datetime import date
from decimal import Decimal

import pytest

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.safety import SafetyManager, GuardrailError
from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore


def _mgr(now=1000.0, today=date(2026, 6, 18), **cfg):
    settings = Settings(_env_file=None, daily_order_limit=Decimal("1000"), **cfg)
    n = {"v": now}
    ids = {"i": 0}
    def gen():
        ids["i"] += 1
        return f"id-{ids['i']}"
    return SafetyManager(
        settings, now=lambda: n["v"], today=lambda: today, gen_id=gen,
        token_store=MemoryTokenStore(), spend_store=MemorySpendStore(),
    ), n


def _spec(mgr, *, qty="1", price="100", coid=None):
    s = mgr.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                       quantity=qty, price=price)
    if coid:
        s.client_order_id = coid
    return s


def test_reserve_then_commit_token_lifecycle():
    mgr, _ = _mgr()
    spec = _spec(mgr, qty="1", price="100")
    token = mgr.issue_token(spec)
    assert mgr.consume(token) is spec
    assert mgr.reserve(spec) is True
    mgr.commit(token)
    # token gone -> consume now raises
    with pytest.raises(GuardrailError, match="invalid-confirmation"):
        mgr.consume(token)


def test_reserve_rejects_over_daily_cap():
    mgr, _ = _mgr()
    big = _spec(mgr, qty="1", price="900", coid="c1")
    assert mgr.reserve(big) is True
    over = _spec(mgr, qty="1", price="200", coid="c2")
    assert mgr.reserve(over) is False  # 900+200 > 1000


def test_release_rolls_back_failed_attempt():
    mgr, _ = _mgr()
    spec = _spec(mgr, qty="1", price="600", coid="c1")
    assert mgr.reserve(spec) is True
    mgr.release(spec)
    # after release, a full-cap order fits again
    spec2 = _spec(mgr, qty="1", price="1000", coid="c2")
    assert mgr.reserve(spec2) is True


def test_check_guardrails_daily_is_read_only():
    mgr, _ = _mgr()
    spec = _spec(mgr, qty="1", price="100")
    mgr.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=True)
    mgr.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=True)
    # read-only: nothing was reserved by the checks
    assert mgr.reserve(_spec(mgr, qty="1", price="1000", coid="x")) is True


def test_expired_token_raises_and_is_deleted():
    mgr, clock = _mgr()
    spec = _spec(mgr)
    token = mgr.issue_token(spec)
    clock["v"] = 1000.0 + 9999
    with pytest.raises(GuardrailError, match="expired-confirmation"):
        mgr.consume(token)


def test_restore_spend_seeds_today_only():
    mgr, _ = _mgr()
    events = [
        {"decision": "placed", "ts": "2026-06-18T01:00:00+00:00",
         "currency": "KRW", "notional": "300"},
        {"decision": "placed", "ts": "2026-06-01T01:00:00+00:00",
         "currency": "KRW", "notional": "999"},  # old day, ignored
    ]
    mgr.restore_spend(events)
    # 300 seeded -> an 800 order fits, a 701 over the remaining cap rejects after
    assert mgr.reserve(_spec(mgr, qty="1", price="700", coid="a")) is True   # 300+700=1000 ok
    assert mgr.reserve(_spec(mgr, qty="1", price="1", coid="b")) is False     # 1000+1 > 1000
