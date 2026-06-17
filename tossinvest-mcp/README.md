# tossinvest-mcp

An **unofficial** MCP server that lets an LLM (Claude Desktop, Cursor, …) read and trade a
Toss Securities account through the `pytossinvest` SDK — behind a safety model designed so the
model cannot YOLO your account.

- **Modes** (`TOSSINVEST_MODE`, default `paper`): `read_only` · `paper` · `live`.
- **Safe by default**: orders go to a local paper portfolio unless you opt into `live`
  (which also requires `TOSSINVEST_ALLOW_LIVE=1`).
- **Two-step orders**: `preview_order` → `place_order` with a confirmation token; guardrails on
  amount, daily total, symbol allow/deny, and high-value confirmation.

## Status

The Toss Open API is in pre-launch. **Paper mode simulates orders/portfolio (no real trades)**
but still reads live market data, so it needs API credentials. The test suite runs fully offline.

> SDK = MIT (`pytossinvest`). This MCP server = Apache-2.0.
