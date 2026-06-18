from decimal import Decimal

import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.safety import SafetyManager
from pytossinvest_mcp.redis_stores import RedisTokenStore, RedisSpendStore
from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore


@pytest.fixture
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _spec():
    from datetime import date
    mgr = SafetyManager(Settings(_env_file=None), now=lambda: 0.0, today=lambda: date(2026, 6, 18),
                        token_store=MemoryTokenStore(), spend_store=MemorySpendStore())
    return mgr.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                          quantity="2", price="70000.5")  # decimal price


def test_token_roundtrip_preserves_decimal(r):
    s = RedisTokenStore(r)
    spec = _spec()
    s.put("t1", spec, expires_at=100.0, issued_at=50.0)
    got_spec, exp, iss = s.get("t1")
    assert (exp, iss) == (100.0, 50.0)
    assert got_spec.notional == spec.notional      # Decimal preserved exactly
    assert got_spec.price == "70000.5"
    assert got_spec.client_order_id == spec.client_order_id
    s.delete("t1")
    assert s.get("t1") is None


def test_spend_reserve_decimal_precise(r):
    s = RedisSpendStore(r)
    assert s.reserve("d", "USD", Decimal("0.1"), Decimal("1"), "c1") is True
    assert s.reserve("d", "USD", Decimal("0.2"), Decimal("1"), "c2") is True
    assert s.current("d", "USD") == Decimal("0.3")  # not 0.30000000000000004


def test_spend_reserve_idempotent_and_cap(r):
    s = RedisSpendStore(r)
    assert s.reserve("d", "KRW", Decimal("900"), Decimal("1000"), "c1") is True
    assert s.reserve("d", "KRW", Decimal("900"), Decimal("1000"), "c1") is True  # idempotent
    assert s.current("d", "KRW") == Decimal("900")
    assert s.reserve("d", "KRW", Decimal("200"), Decimal("1000"), "c2") is False


def test_spend_release_idempotent_floor(r):
    s = RedisSpendStore(r)
    s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1")
    s.release("d", "KRW", Decimal("100"), "c1")
    s.release("d", "KRW", Decimal("100"), "c1")  # idempotent
    assert s.current("d", "KRW") == Decimal("0")


def test_two_managers_share_token(r):
    from datetime import date
    cfg = Settings(_env_file=None)
    mk = lambda: SafetyManager(cfg, now=lambda: 0.0, today=lambda: date(2026, 6, 18),
                               gen_id=lambda: "fixed-token",
                               token_store=RedisTokenStore(r), spend_store=RedisSpendStore(r))
    a, b = mk(), mk()
    spec = a.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="1", price="100")
    token = a.issue_token(spec)
    # instance B can consume the token instance A issued
    got = b.consume(token)
    assert got.client_order_id == spec.client_order_id
