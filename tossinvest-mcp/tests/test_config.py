from decimal import Decimal

import pytest

from tossinvest_mcp.config import Settings


def _settings(**kw):
    # _env_file=None so a stray local .env never leaks into tests
    return Settings(_env_file=None, **kw)


def test_defaults_are_safe():
    s = _settings()
    assert s.mode == "paper"
    assert s.allow_live is False
    assert s.use_paper is True
    assert s.is_live is False


def test_money_fields_are_decimal_from_str():
    s = _settings(max_order_amount="2000000", daily_order_limit="9000000")
    assert s.max_order_amount == Decimal("2000000")
    assert isinstance(s.max_order_amount, Decimal)


def test_money_fields_reject_float():
    with pytest.raises(Exception):
        _settings(max_order_amount=1000000.5)


def test_live_without_allow_live_is_rejected():
    with pytest.raises(ValueError):
        _settings(mode="live", allow_live=False)


def test_live_with_allow_live_ok():
    s = _settings(mode="live", allow_live=True)
    assert s.is_live is True
    assert s.use_paper is False


def test_read_only_mode():
    s = _settings(mode="read_only")
    assert s.use_paper is False
    assert s.is_live is False


def test_usd_caps_default_and_decimal():
    s = _settings()
    assert s.max_order_amount_usd == Decimal("1000")
    assert s.daily_order_limit_usd == Decimal("5000")
    assert isinstance(s.max_order_amount_usd, Decimal)


def test_usd_caps_reject_float():
    with pytest.raises(Exception):
        _settings(max_order_amount_usd=1000.5)
