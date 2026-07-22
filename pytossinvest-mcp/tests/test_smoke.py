import asyncio

import pytossinvest_mcp


def test_version_exposed():
    assert pytossinvest_mcp.__version__ == "0.0.1"


def test_mcpserver_harness_works():
    """Prove we can register a tool and read it back via list_tools() in-process."""
    from mcp.server import MCPServer

    mcp = MCPServer("harness-check")

    @mcp.tool(name="ping", description="returns pong")
    def ping() -> dict:
        return {"reply": "pong"}

    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == {"ping"}
