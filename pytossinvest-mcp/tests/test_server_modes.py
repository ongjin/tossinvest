import asyncio
from decimal import Decimal

import pytest

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.server import build_server, build_app_context
from conftest import FakeClient  # reuse the fake (pytest puts tests/ on sys.path)

READ_TOOLS = {"get_accounts", "get_holdings", "get_buying_power", "get_quote", "get_candles",
              "get_stock_info", "get_market_info", "list_orders", "get_order"}
WRITE_TOOLS = {"get_order_readiness", "preview_order", "place_order",
               "preview_modify", "modify_order", "cancel_order"}


def _build(tmp_path, mode, **kw):
    settings = Settings(_env_file=None, mode=mode,
                        audit_log_path=str(tmp_path / "audit.log"), **kw)
    return build_server(settings, client=FakeClient())


def _names(mcp):
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_read_only_registers_reads_only(tmp_path):
    mcp = _build(tmp_path, "read_only")
    assert _names(mcp) == READ_TOOLS


def test_paper_registers_reads_and_writes(tmp_path):
    mcp = _build(tmp_path, "paper")
    assert _names(mcp) == READ_TOOLS | WRITE_TOOLS


def test_live_registers_reads_and_writes(tmp_path):
    mcp = _build(tmp_path, "live", allow_live=True)
    assert _names(mcp) == READ_TOOLS | WRITE_TOOLS


def test_call_tool_smoke_paper(tmp_path):
    mcp = _build(tmp_path, "paper")
    # in-process call: should run the closure without raising
    result = asyncio.run(mcp.call_tool("get_accounts", {}))
    assert result is not None


def test_build_server_restores_todays_spend(tmp_path):
    from datetime import datetime, timezone
    audit_path = tmp_path / "audit.log"
    ts = datetime.now(timezone.utc).isoformat()
    audit_path.write_text(
        f'{{"ts": "{ts}", "tool": "place_order", "decision": "placed", '
        f'"notional": "700000", "currency": "KRW"}}\n', encoding="utf-8")
    from zoneinfo import ZoneInfo
    settings = Settings(_env_file=None, mode="paper", audit_log_path=str(audit_path))
    app = build_app_context(settings, client=FakeClient())
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    assert app.safety.spend_store.current(today, "KRW") == Decimal("700000")


class _DummyClient:
    pass


def test_memory_backend_uses_memory_stores(tmp_path):
    s = Settings(_env_file=None, audit_log_path=str(tmp_path / "a.log"))
    app = build_app_context(s, client=_DummyClient())
    from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore
    assert isinstance(app.safety.token_store, MemoryTokenStore)
    assert isinstance(app.safety.spend_store, MemorySpendStore)


def test_redis_backend_uses_redis_stores(tmp_path, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import pytossinvest_mcp.server as srv
    monkeypatch.setattr(srv, "_redis_from_url",
                        lambda url: fakeredis.FakeStrictRedis(decode_responses=True))
    s = Settings(_env_file=None, state_backend="redis", redis_url="redis://x")
    app = build_app_context(s, client=_DummyClient())
    from pytossinvest_mcp.redis_stores import RedisTokenStore, RedisSpendStore
    assert isinstance(app.safety.token_store, RedisTokenStore)
    assert isinstance(app.safety.spend_store, RedisSpendStore)


def test_redis_down_fails_closed(tmp_path, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import pytossinvest_mcp.server as srv
    from pytossinvest_mcp.safety import GuardrailError

    class _BrokenRedis(fakeredis.FakeStrictRedis):
        def get(self, *a, **k):
            raise ConnectionError("redis down")
        def lock(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(srv, "_redis_from_url",
                        lambda url: _BrokenRedis(decode_responses=True))
    s = Settings(_env_file=None, state_backend="redis", redis_url="redis://x")
    app = build_app_context(s, client=_DummyClient())
    spec = app.safety.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                                 quantity="1", price="100")
    with pytest.raises(GuardrailError, match="state-unavailable"):
        app.safety.reserve(spec)


def test_confirmation_expiry_uses_wall_clock(tmp_path):
    # tokens can live in redis: an epoch deadline stays valid across host restarts and hosts,
    # a monotonic one does not
    import time
    settings = Settings(_env_file=None, mode="paper", audit_log_path=str(tmp_path / "audit.log"))
    app = build_app_context(settings, client=FakeClient())
    spec = app.safety.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                                 quantity="1", price="70000")
    token = app.safety.issue_token(spec)
    _, expires_at, _ = app.safety.token_store.get(token)
    assert abs(expires_at - (time.time() + settings.confirmation_ttl_sec)) < 5


class _RejectingClient(FakeClient):
    def get_holdings(self, symbol=None):
        from pytossinvest.errors import BusinessRuleError
        raise BusinessRuleError("account-restricted", "account is restricted", http_status=422)


@pytest.mark.parametrize("mode, client, tool, args, expected", [
    ("paper", FakeClient(), "place_order", {"confirmation_token": "nope"}, "invalid-confirmation"),
    ("paper", FakeClient(), "cancel_order", {"order_id": "x"}, "live-only"),
    ("paper", FakeClient(), "get_order", {"order_id": "nope"}, "paper order not found"),
    ("read_only", _RejectingClient(), "get_holdings", {}, "account-restricted"),
])
def test_domain_errors_reach_the_model(tmp_path, mode, client, tool, args, expected):
    # SDK >= 2.1 shows the model only "Error executing tool <name>" for non-ToolError exceptions
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    settings = Settings(_env_file=None, mode=mode, audit_log_path=str(tmp_path / "audit.log"))
    mcp = build_server(settings, client=client)
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp.call_tool(tool, args))
    assert not isinstance(e.value, UnexpectedToolError)
    assert expected in str(e.value)


def _props(mcp, tool):
    return {t.name: t for t in asyncio.run(mcp.list_tools())}[tool].input_schema["properties"]


def test_order_params_advertise_enums(tmp_path):
    mcp = _build(tmp_path, "paper")
    preview = _props(mcp, "preview_order")
    assert preview["side"]["enum"] == ["BUY", "SELL"]
    assert preview["order_type"]["enum"] == ["LIMIT", "MARKET"]
    assert preview["time_in_force"]["enum"] == ["DAY", "CLS", "OPG"]
    assert _props(mcp, "preview_modify")["order_type"]["enum"] == ["LIMIT", "MARKET"]
    assert _props(mcp, "list_orders")["status"]["enum"] == ["OPEN", "CLOSED"]


def test_lowercase_side_is_rejected_with_a_visible_reason(tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    mcp = _build(tmp_path, "paper")
    args = {"symbol": "005930", "side": "buy", "order_type": "LIMIT", "quantity": "1", "price": "70000"}
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp.call_tool("preview_order", args))
    assert not isinstance(e.value, UnexpectedToolError)
    assert "BUY" in str(e.value)


class _TimeoutClient(FakeClient):
    def get_holdings(self, symbol=None):
        import httpx
        raise httpx.ReadTimeout("read timed out")


def test_network_timeouts_reach_the_model(tmp_path):
    # after an ambiguous place failure the model must know it was a timeout to retry the token
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    settings = Settings(_env_file=None, mode="read_only", audit_log_path=str(tmp_path / "audit.log"))
    mcp = build_server(settings, client=_TimeoutClient())
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp.call_tool("get_holdings", {}))
    assert not isinstance(e.value, UnexpectedToolError)
    assert "timed out" in str(e.value)
