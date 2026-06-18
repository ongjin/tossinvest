import asyncio
from decimal import Decimal

import pytest

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.server import build_server, build_app_context
from conftest import FakeClient  # reuse the fake (pytest puts tests/ on sys.path)

READ_TOOLS = {"get_accounts", "get_holdings", "get_quote", "get_candles",
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
