from datetime import date
from decimal import Decimal

import pytest

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.safety import SafetyManager, GuardrailError, order_currency
from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore


def _ids():
    n = {"i": 0}
    def gen():
        n["i"] += 1
        return f"cli-{n['i']}"
    return gen


def _mgr(**overrides):
    s = Settings(_env_file=None, **overrides)
    return SafetyManager(
        s, now=lambda: 1000.0, today=lambda: date(2026, 6, 17), gen_id=_ids(),
        token_store=MemoryTokenStore(), spend_store=MemorySpendStore(),
    )


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
    assert m.reserve(s1) is True  # simulate successful place
    s2 = _spec(m, quantity="10", price="70000")  # +700,000 -> 1,400,000 > 1,000,000
    assert m.reserve(s2) is False  # daily-limit: reserve rejects, not check_guardrails


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


def test_order_currency_alpha_is_usd_numeric_is_krw():
    assert order_currency("AAPL") == "USD"
    assert order_currency("005930") == "KRW"


def test_build_spec_sets_currency_and_modify_id():
    m = _mgr()
    krw = m.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="1", price="70000")
    usd = m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET", order_amount="100",
                       modify_order_id="ord-9")
    assert krw.currency == "KRW" and krw.modify_order_id is None
    assert usd.currency == "USD" and usd.modify_order_id == "ord-9"


def _usd_spec(m, **kw):
    base = dict(symbol="AAPL", side="BUY", order_type="LIMIT", quantity="1", price="100")
    base.update(kw)
    return m.build_spec(**base)


def test_usd_per_order_cap_uses_usd_threshold():
    m = _mgr(max_order_amount="1000000", max_order_amount_usd="1000")
    spec = _usd_spec(m, quantity="20", price="100")  # $2,000 > $1,000 cap (KRW cap irrelevant)
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "order-amount-cap"


def test_usd_high_value_threshold_is_100k_usd():
    m = _mgr(max_order_amount_usd="999999999", daily_order_limit_usd="999999999")
    spec = _usd_spec(m, quantity="2000", price="100")  # $200,000 >= $100,000
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "confirm-high-value-required"


def test_usd_hard_ceiling_is_3m_usd():
    m = _mgr(max_order_amount_usd="999999999", daily_order_limit_usd="999999999")
    spec = _usd_spec(m, quantity="40000", price="100", confirm_high_value_order=True)  # $4,000,000 > $3,000,000
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "max-order-exceeded"


def test_daily_buckets_are_per_currency():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000",
             max_order_amount_usd="9000", daily_order_limit_usd="9000")
    krw = _spec(m, quantity="10", price="70000")  # 700,000 KRW
    m.check_guardrails(krw, is_market_open=True, enforce_hours=False)
    assert m.reserve(krw) is True  # simulate successful place
    # a USD order is unaffected by the KRW bucket being near its limit
    usd = _usd_spec(m, quantity="1", price="100")  # $100
    m.check_guardrails(usd, is_market_open=True, enforce_hours=False)  # must NOT raise
    # but a second KRW order tips the KRW bucket over
    krw2 = _spec(m, quantity="10", price="70000")
    assert m.reserve(krw2) is False  # daily-limit: reserve rejects


def test_check_daily_false_skips_daily_gate():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    day = date(2026, 6, 17).isoformat()
    m.spend_store.seed(day, "KRW", Decimal("900000"))
    spec = _spec(m, quantity="10", price="70000")  # +700,000 -> over 1,000,000
    # default would reject via reserve; check_daily=False skips it (other gates still run)
    m.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=False)


def test_build_spec_rejects_order_amount_with_price():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="AAPL", side="BUY", order_type="LIMIT",
                     order_amount="100", price="1000000", quantity="1000")
    assert e.value.code == "invalid-order-params"


def test_build_spec_rejects_order_amount_with_quantity():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET",
                     order_amount="100", quantity="1000")
    assert e.value.code == "invalid-order-params"


def test_deny_list_matches_whitespace_and_case_insensitive():
    m = _mgr(deny_symbols=["AAPL"])
    with pytest.raises(GuardrailError) as e:
        _ok(m, _spec(m, symbol=" aapl "))  # evasion attempt
    assert e.value.code == "symbol-denied"


def test_allow_list_normalizes_symbol():
    m = _mgr(allow_symbols=["aapl"], max_order_amount_usd="999999999",
             daily_order_limit_usd="999999999")  # config lowercase
    _ok(m, _spec(m, symbol="AAPL"))       # must pass (normalized match)


def test_deny_list_blocks_unicode_whitespace_and_fullwidth_evasion():
    m = _mgr(deny_symbols=["AAPL"])
    for evasion in [" AAPL", "AAPL​", "ＡＡＰＬ"]:  # NBSP, zero-width, fullwidth
        with pytest.raises(GuardrailError) as e:
            _ok(m, _spec(m, symbol=evasion, price="100"))
        assert e.value.code == "symbol-denied", evasion


def test_canon_symbol_keeps_legit_dotted_symbol():
    m = _mgr(deny_symbols=["BRK.B"])
    with pytest.raises(GuardrailError) as e:
        _ok(m, _spec(m, symbol="brk.b", price="100"))   # normalized match
    assert e.value.code == "symbol-denied"
    # an unrelated dotted symbol is not blocked
    _ok(m, _spec(m, symbol="BF.B"))


def test_build_spec_explicit_currency_overrides_symbol_shape():
    m = _mgr()
    # numeric symbol would default to KRW, but explicit currency wins
    spec = m.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="1", price="100", currency="USD")
    assert spec.currency == "USD"


def test_build_spec_currency_none_falls_back_to_symbol_shape():
    m = _mgr()
    spec = m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET",
                        order_amount="100", currency=None)
    assert spec.currency == "USD"  # symbol-shape fallback


def test_daily_check_uses_delta_when_prev_notional_given():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    day = date(2026, 6, 17).isoformat()
    m.spend_store.seed(day, "KRW", Decimal("950000"))  # bucket near cap
    # amended order: new=710,000, prev=700,000 -> delta=+10,000 -> 960,000 <= 1,000,000 OK
    spec = _spec(m, quantity="10", price="71000")  # notional 710,000
    m.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                       prev_notional=Decimal("700000"))  # must NOT raise


def test_daily_check_delta_still_rejects_when_over_cap():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    day = date(2026, 6, 17).isoformat()
    m.spend_store.seed(day, "KRW", Decimal("950000"))
    # new=900,000, prev=100,000 -> delta=+800,000 -> 1,750,000 > 1,000,000 -> reject
    spec = _spec(m, quantity="10", price="90000")  # notional 900,000
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                           prev_notional=Decimal("100000"))
    assert e.value.code == "daily-limit"


def test_per_order_cap_uses_full_notional_not_delta():
    m = _mgr(max_order_amount="500000", daily_order_limit="999999999")
    # delta tiny but full new notional exceeds per-order cap
    spec = _spec(m, quantity="10", price="71000")  # 710,000 > 500,000 cap
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                           prev_notional=Decimal("700000"))
    assert e.value.code == "order-amount-cap"
