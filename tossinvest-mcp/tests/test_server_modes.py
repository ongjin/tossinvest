import asyncio
from decimal import Decimal

from tossinvest_mcp.config import Settings
from tossinvest_mcp.server import build_server, build_app_context
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
    from tossinvest_mcp.server import build_app_context
    audit_path = tmp_path / "audit.log"
    ts = datetime.now(timezone.utc).isoformat()
    audit_path.write_text(
        f'{{"ts": "{ts}", "tool": "place_order", "decision": "placed", '
        f'"notional": "700000", "currency": "KRW"}}\n', encoding="utf-8")
    settings = Settings(_env_file=None, mode="paper", audit_log_path=str(audit_path))
    app = build_app_context(settings, client=FakeClient())
    assert app.safety._spent["KRW"] == Decimal("700000")
