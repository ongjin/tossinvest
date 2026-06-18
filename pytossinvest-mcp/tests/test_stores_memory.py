from decimal import Decimal

from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore


def test_token_put_get_delete():
    s = MemoryTokenStore()
    s.put("t1", "SPEC", expires_at=100.0, issued_at=50.0)
    assert s.get("t1") == ("SPEC", 100.0, 50.0)
    s.delete("t1")
    assert s.get("t1") is None
    assert s.get("missing") is None


def test_spend_reserve_under_cap():
    s = MemorySpendStore()
    assert s.reserve("2026-06-18", "KRW", Decimal("100"), Decimal("1000"), "c1") is True
    assert s.current("2026-06-18", "KRW") == Decimal("100")


def test_spend_reserve_over_cap_rejects_without_counting():
    s = MemorySpendStore()
    assert s.reserve("d", "KRW", Decimal("900"), Decimal("1000"), "c1") is True
    assert s.reserve("d", "KRW", Decimal("200"), Decimal("1000"), "c2") is False
    assert s.current("d", "KRW") == Decimal("900")  # rejected one not counted


def test_spend_reserve_is_idempotent_by_dedup():
    s = MemorySpendStore()
    assert s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1") is True
    assert s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1") is True  # same key
    assert s.current("d", "KRW") == Decimal("100")  # counted once


def test_spend_release_rolls_back_existing_only():
    s = MemorySpendStore()
    s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1")
    s.release("d", "KRW", Decimal("100"), "c1")
    assert s.current("d", "KRW") == Decimal("0")
    s.release("d", "KRW", Decimal("100"), "c1")  # idempotent, no underflow
    assert s.current("d", "KRW") == Decimal("0")
    # re-reserve after release works (fresh attempt)
    assert s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1") is True


def test_spend_negative_delta_downsize_and_release():
    s = MemorySpendStore()
    s.reserve("d", "KRW", Decimal("500"), Decimal("1000"), "base")
    # modify downsize: delta = -200
    assert s.reserve("d", "KRW", Decimal("-200"), Decimal("1000"), "m1") is True
    assert s.current("d", "KRW") == Decimal("300")
    s.release("d", "KRW", Decimal("-200"), "m1")  # rollback adds back
    assert s.current("d", "KRW") == Decimal("500")


def test_spend_seed_is_floored():
    s = MemorySpendStore()
    s.seed("d", "KRW", Decimal("100"))
    s.seed("d", "KRW", Decimal("50"))
    assert s.current("d", "KRW") == Decimal("150")
    s.seed("d", "KRW", Decimal("-1000"))  # floored at 0
    assert s.current("d", "KRW") == Decimal("0")
