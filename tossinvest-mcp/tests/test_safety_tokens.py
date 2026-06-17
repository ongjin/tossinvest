from datetime import date
from decimal import Decimal

import pytest

from tossinvest_mcp.config import Settings
from tossinvest_mcp.safety import SafetyManager, GuardrailError


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _mgr(clock, **overrides):
    s = Settings(_env_file=None, confirmation_ttl_sec=120, **overrides)
    return SafetyManager(s, now=clock, today=lambda: date(2026, 6, 17))


def _spec(m):
    return m.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="10", price="70000")


def test_issue_then_consume_returns_spec():
    clock = Clock()
    m = _mgr(clock)
    spec = _spec(m)
    token = m.issue_token(spec)
    got = m.consume(token)
    assert got.client_order_id == spec.client_order_id


def test_unknown_token_rejected():
    m = _mgr(Clock())
    with pytest.raises(GuardrailError) as e:
        m.consume("does-not-exist")
    assert e.value.code == "invalid-confirmation"


def test_expired_token_rejected():
    clock = Clock()
    m = _mgr(clock)
    token = m.issue_token(_spec(m))
    clock.advance(121)  # ttl is 120
    with pytest.raises(GuardrailError) as e:
        m.consume(token)
    assert e.value.code == "expired-confirmation"


def test_finalize_consumes_token_and_records_spend():
    clock = Clock()
    m = _mgr(clock, daily_order_limit="999999999")
    spec = _spec(m)
    token = m.issue_token(spec)
    m.consume(token)
    m.finalize(token, spec.notional)
    # second consume fails: token gone (no double-fire)
    with pytest.raises(GuardrailError) as e:
        m.consume(token)
    assert e.value.code == "invalid-confirmation"
    # spend was recorded toward the daily cap
    assert m._spent["KRW"] == Decimal("700000")


def test_failed_place_leaves_token_for_idempotent_retry():
    clock = Clock()
    m = _mgr(clock)
    spec = _spec(m)
    token = m.issue_token(spec)
    # simulate place attempt that consumes (validates) but does NOT finalize (failed)
    first = m.consume(token)
    # retry: same token still valid, same clientOrderId reused
    second = m.consume(token)
    assert first.client_order_id == second.client_order_id


def _live_mgr(clock, **overrides):
    s = Settings(_env_file=None, mode="live", allow_live=True,
                 confirmation_ttl_sec=120, **overrides)
    return SafetyManager(s, now=clock, today=lambda: date(2026, 6, 17))


def test_live_min_delay_blocks_immediate_consume_then_allows():
    clock = Clock()
    m = _live_mgr(clock, live_confirm_min_delay_sec=5)
    token = m.issue_token(_spec(m))
    with pytest.raises(GuardrailError) as e:
        m.consume(token)  # 0s since issue, < 5
    assert e.value.code == "confirm-too-soon"
    clock.advance(5)
    assert m.consume(token).client_order_id  # now allowed


def test_min_delay_off_by_default_even_in_live():
    clock = Clock()
    m = _live_mgr(clock)  # live_confirm_min_delay_sec defaults 0
    token = m.issue_token(_spec(m))
    assert m.consume(token).client_order_id  # immediate consume OK


def test_release_pops_without_recording_spend():
    clock = Clock()
    m = _mgr(clock, daily_order_limit="999999999")
    spec = _spec(m)
    token = m.issue_token(spec)
    m.release(token)
    assert m._spent["KRW"] == Decimal("0")  # NOT recorded
    with pytest.raises(GuardrailError) as e:
        m.consume(token)  # token gone
    assert e.value.code == "invalid-confirmation"
