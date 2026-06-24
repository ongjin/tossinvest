from decimal import Decimal

import pytest

from pytossinvest_mcp.config import Settings


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


def test_live_confirm_min_delay_default_zero():
    assert _settings().live_confirm_min_delay_sec == 0


def test_state_backend_defaults_to_memory():
    s = _settings()
    assert s.state_backend == "memory"
    assert s.redis_url == ""


def test_redis_backend_requires_url():
    with pytest.raises(ValueError, match="TOSSINVEST_REDIS_URL"):
        _settings(state_backend="redis")


def test_redis_backend_with_url_ok():
    s = _settings(state_backend="redis", redis_url="redis://localhost:6379/0")
    assert s.state_backend == "redis"


def test_transport_defaults_to_stdio():
    s = Settings(_env_file=None)
    assert s.transport == "stdio"
    assert s.http_host == "127.0.0.1"
    assert s.http_port == 8000
    assert s.auth_token == ""


def test_http_without_auth_token_raises():
    with pytest.raises(ValueError, match="TOSSINVEST_AUTH_TOKEN"):
        Settings(_env_file=None, transport="http")


def test_http_with_auth_token_ok():
    s = Settings(_env_file=None, transport="http", auth_token="secret",
                 http_host="0.0.0.0", http_port=9000)
    assert s.transport == "http"
    assert s.http_host == "0.0.0.0"
    assert s.http_port == 9000


def test_stdio_needs_no_auth_token():
    s = Settings(_env_file=None, transport="stdio")  # must not raise
    assert s.transport == "stdio"


def test_http_allowed_hosts_defaults_empty():
    s = Settings(_env_file=None)
    assert s.http_allowed_hosts == []


def test_http_allowed_hosts_can_be_set():
    s = Settings(_env_file=None, transport="http", auth_token="secret",
                 http_allowed_hosts=["mcp.example.com", "mcp.example.com:*"])
    assert s.http_allowed_hosts == ["mcp.example.com", "mcp.example.com:*"]


def test_paper_starting_cash_default():
    assert _settings().paper_starting_cash == {"KRW": Decimal("10000000")}


def test_paper_starting_cash_dict():
    s = _settings(paper_starting_cash={"KRW": "10000000", "USD": "7000"})
    assert s.paper_starting_cash == {"KRW": Decimal("10000000"), "USD": Decimal("7000")}


def test_paper_starting_cash_scalar_wraps_krw():
    s = _settings(paper_starting_cash="5000000")
    assert s.paper_starting_cash == {"KRW": Decimal("5000000")}


def test_paper_starting_cash_rejects_float_value():
    with pytest.raises(TypeError):
        _settings(paper_starting_cash={"USD": 7000.5})


def test_paper_starting_cash_rejects_float_scalar():
    with pytest.raises(TypeError):
        _settings(paper_starting_cash=1000.5)


def test_paper_starting_cash_from_env_json(monkeypatch):
    monkeypatch.setenv("TOSSINVEST_PAPER_STARTING_CASH", '{"KRW":"10000000","USD":"7000"}')
    s = Settings(_env_file=None)
    assert s.paper_starting_cash == {"KRW": Decimal("10000000"), "USD": Decimal("7000")}


def test_paper_starting_cash_legacy_scalar_env(monkeypatch):
    monkeypatch.setenv("TOSSINVEST_PAPER_STARTING_CASH", "5000000")
    s = Settings(_env_file=None)
    assert s.paper_starting_cash == {"KRW": Decimal("5000000")}
