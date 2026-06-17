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

## Tools

Read (always): `get_accounts`, `get_holdings`, `get_quote`, `get_candles`, `get_stock_info`,
`get_market_info`, `list_orders`, `get_order`.
Write (paper/live only): `get_order_readiness`, `preview_order` → `place_order`, `modify_order`,
`cancel_order`.

## Configure (env, prefix `TOSSINVEST_`)

| var | default | meaning |
|---|---|---|
| `TOSSINVEST_MODE` | `paper` | `read_only` · `paper` · `live` |
| `TOSSINVEST_ALLOW_LIVE` | `0` | must be `1` for `live` to start |
| `TOSSINVEST_CLIENT_ID` / `_SECRET` | — | Toss Open API credentials |
| `TOSSINVEST_MAX_ORDER_AMOUNT` | `1000000` | per-order cap |
| `TOSSINVEST_DAILY_ORDER_LIMIT` | `5000000` | cumulative daily cap |
| `TOSSINVEST_DENY_SYMBOLS` / `_ALLOW_SYMBOLS` | `[]` | JSON list, e.g. `["005930"]` |
| `TOSSINVEST_ENFORCE_MARKET_HOURS` | `1` | live-only hours gate |

## Run (Claude Desktop `claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "tossinvest": {
      "command": "uv",
      "args": ["run", "--package", "tossinvest-mcp", "tossinvest-mcp"],
      "env": { "TOSSINVEST_MODE": "paper",
               "TOSSINVEST_CLIENT_ID": "...", "TOSSINVEST_CLIENT_SECRET": "..." }
    }
  }
}
```

### Ordering is two-step

1. `preview_order(...)` → returns `confirmationToken` (+ estimated notional, guardrail check).
2. `place_order(confirmation_token=...)` → executes. Without a valid token, `place_order` refuses.

To trade for real: set `TOSSINVEST_MODE=live` **and** `TOSSINVEST_ALLOW_LIVE=1`.
