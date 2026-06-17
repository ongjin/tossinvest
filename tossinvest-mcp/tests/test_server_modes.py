import asyncio

from tossinvest_mcp.config import Settings
from tossinvest_mcp.server import build_server
from conftest import FakeClient  # reuse the fake (pytest puts tests/ on sys.path)

READ_TOOLS = {"get_accounts", "get_holdings", "get_quote", "get_candles",
              "get_stock_info", "get_market_info", "list_orders", "get_order"}
WRITE_TOOLS = {"get_order_readiness", "preview_order", "place_order",
               "modify_order", "cancel_order"}


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
