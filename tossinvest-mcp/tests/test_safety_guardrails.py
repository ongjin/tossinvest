from datetime import date
from decimal import Decimal

import pytest

from tossinvest_mcp.config import Settings
from tossinvest_mcp.safety import SafetyManager, GuardrailError


def _ids():
    n = {"i": 0}
    def gen():
        n["i"] += 1
        return f"cli-{n['i']}"
    return gen


def _mgr(**overrides):
    s = Settings(_env_file=None, **overrides)
    return SafetyManager(s, now=lambda: 1000.0, today=lambda: date(2026, 6, 17), gen_id=_ids())


def test_build_spec_notional_quantity_based():
    m = _mgr()
    spec = m.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="10", price="70000")
    assert spec.notional == Decimal("700000")
    assert spec.client_order_id == "cli-1"


def test_build_spec_notional_amount_based():
    m = _mgr()
    spec = m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET", order_amount="100")
    assert spec.notional == Decimal("100")


def test_build_spec_market_quantity_uses_ref_price():
    m = _mgr()
    spec = m.build_spec(symbol="005930", side="BUY", order_type="MARKET",
                        quantity="3", ref_price="70000")
    assert spec.notional == Decimal("210000")


def test_build_spec_insufficient_params():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="005930", side="BUY", order_type="MARKET", quantity="3")
    assert e.value.code == "insufficient-order-params"


def _spec(m, **kw):
    base = dict(symbol="005930", side="BUY", order_type="LIMIT", quantity="1", price="70000")
    base.update(kw)
    return m.build_spec(**base)


def _ok(m, spec):
    m.check_guardrails(spec, is_market_open=True, enforce_hours=False)


def test_per_order_cap_rejects():
    m = _mgr(max_order_amount="1000000")
    spec = _spec(m, quantity="20", price="70000")  # 1,400,000 > cap
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "order-amount-cap"


def test_within_cap_passes():
    m = _mgr(max_order_amount="1000000")
    _ok(m, _spec(m, quantity="10", price="70000"))  # 700,000


def test_deny_list_rejects():
    m = _mgr(deny_symbols=["005930"])
    with pytest.raises(GuardrailError) as e:
        _ok(m, _spec(m))
    assert e.value.code == "symbol-denied"


def test_allow_list_rejects_others():
    m = _mgr(allow_symbols=["000660"])
    with pytest.raises(GuardrailError) as e:
        _ok(m, _spec(m, symbol="005930"))
    assert e.value.code == "symbol-not-allowed"


def test_high_value_requires_confirm():
    m = _mgr(max_order_amount="999999999999", daily_order_limit="999999999999")
    spec = _spec(m, quantity="2000", price="70000")  # 140,000,000 >= 1억
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "confirm-high-value-required"


def test_high_value_with_confirm_passes():
    m = _mgr(max_order_amount="999999999999", daily_order_limit="999999999999")
    spec = _spec(m, quantity="2000", price="70000", confirm_high_value_order=True)
    _ok(m, spec)


def test_above_max_threshold_always_rejected():
    m = _mgr(max_order_amount="999999999999999", daily_order_limit="999999999999999")
    spec = _spec(m, quantity="100000", price="70000", confirm_high_value_order=True)  # 7,000,000,000 > 30억
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "max-order-exceeded"


def test_daily_limit_accumulates():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    s1 = _spec(m, quantity="10", price="70000")  # 700,000
    m.check_guardrails(s1, is_market_open=True, enforce_hours=False)
    m.record_spend(s1.notional)
    s2 = _spec(m, quantity="10", price="70000")  # +700,000 -> 1,400,000 > 1,000,000
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(s2, is_market_open=True, enforce_hours=False)
    assert e.value.code == "daily-limit"


def test_market_closed_rejected_when_enforced():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(_spec(m), is_market_open=False, enforce_hours=True)
    assert e.value.code == "market-closed"


def test_build_spec_rejects_nonpositive_quantity():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="0", price="70000")
    assert e.value.code == "invalid-order-value"


def test_build_spec_rejects_negative_price():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="10", price="-1")
    assert e.value.code == "invalid-order-value"


def test_build_spec_rejects_nonpositive_order_amount():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET", order_amount="0")
    assert e.value.code == "invalid-order-value"
