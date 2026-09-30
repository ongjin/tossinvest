# Conditional orders over MCP — design

**Date:** 2026-09-30
**Status:** approved (design), pending implementation plan
**Scope:** `pytossinvest-mcp` (new `conditional.py`, `safety.OrderSpec`, `tools.place_order`/`modify_order` dispatch, `server.py` registration, redis token serialization, tests, docs). SDK already wraps the endpoints (`create/modify/cancel/list/get_conditional_order`, 2026-09-30).

## Problem

Toss Open API 1.2.19 added conditional orders (SINGLE / OCO / OTO): an order the broker fires by itself when a trigger price is reached, until `expireDate`. The SDK wraps them, but the MCP server exposes nothing, so an LLM client can neither see the stop-loss / take-profit orders the user set in the app nor create one. Creating one is an order path, so it must sit behind the same safety model as `place_order` (guardrails, preview → confirm token, daily cap, idempotency, audit).

## Goals

- Read conditional orders in every mode that reads the real account.
- Create / modify / cancel them in `live`, with no path around `check_guardrails` and the confirmation token.
- Keep one execution path for the safety invariant: consume token → guardrails → reserve → execute → commit / release.
- Readable rejections for malformed input before a token is issued.

## Non-goals (YAGNI)

- Paper simulation of conditional orders (the paper engine fills immediately; there is no trigger watcher).
- Re-implementing Toss's semantic rules locally (OCO both SELL, trigger vs current price, OCO/OTO LIMIT-only, one OCO/OTO per symbol). Toss rejects those with a 4xx the model can read.
- `PROFIT_RATE` conditions on create (the create request only takes `triggerPrice`).
- Streaming (`pytossinvest.stream`) over MCP: tools are request/response.

## Chosen approach

**Reuse the two-step tools (Approach A).** Two new preview tools issue confirmation tokens whose spec is marked `kind="conditional"`; the existing `place_order` / `modify_order` execute them and branch on `kind` only at the execution step. Rejected: a separate `place_conditional_order` / `modify_conditional_order` pair (duplicates reserve/commit/release, 22 tools); extending `preview_order` with condition params (bloats a tool models already use correctly).

## Tools and modes

| mode | `list_conditional_orders`, `get_conditional_order` | `preview_conditional_order`, `preview_conditional_modify`, `cancel_conditional_order` |
|---|---|---|
| `read_only` | real account | not registered |
| `paper` | empty list / not found (account reads are paper-routed) | `PaperError`: live-only |
| `live` | real account | real account |

Tool count 15 → 20.

Parameters are flat so schemas carry enums and the model does not build nested objects:

- `preview_conditional_order(symbol, type: SINGLE|OCO|OTO, quantity, order_type: LIMIT|MARKET, expire_date: YYYY-MM-DD, first_side: BUY|SELL, first_trigger_price, first_order_price=None, second_side=None, second_trigger_price=None, second_order_price=None, confirm_high_value_order=False)`
- `preview_conditional_modify(conditional_order_id, <same minus symbol>)` — the API resets the whole order and returns a new id.
- `cancel_conditional_order(conditional_order_id)` — direct (cancelling lowers risk), audited, like `cancel_order`.
- `list_conditional_orders(status: OPEN|CLOSED = OPEN, symbol=None, cursor=None)`, `get_conditional_order(conditional_order_id)`.
- Descriptions say: listings include conditional orders made in other channels (the app); modify returns a new id; place / modify go through `place_order` / `modify_order` with the returned token.

## Validation (before a token is issued)

`GuardrailError` codes the model can read:

- `invalid-order-value`: `quantity`, each trigger price, each order price must be finite and > 0 (same `_is_positive_number` as `build_spec`).
- `invalid-order-params`: `second_*` required for OCO / OTO and forbidden for SINGLE; order prices required for LIMIT and forbidden for MARKET; `expire_date` must be an ISO date.

Everything else is Toss's call.

## Notional and guardrails

- Leg price = `orderPrice` (LIMIT) or `triggerPrice` (MARKET). Leg notional = `quantity × leg price`.
- SINGLE = first leg. **OCO = max(first, second)** (only one fires). **OTO = first + second** (both fire).
- Currency: the authoritative quote currency (`_price_and_currency`), else `order_currency(symbol)` fallback — same as `preview_order`.
- `check_guardrails` unchanged: deny / allow, hard ceiling, high-value confirm, per-order cap, daily cap. **Market-hours gate skipped**: registering is allowed any time; the broker only fires during sessions.

## Daily-cap accounting (user decision: count on registration day)

- `place_order` on a conditional token reserves the **full notional on the registration day** (KST), whether or not it ever fires.
- `modify_order` on a conditional modify token reserves the **delta** `new − old`. `old` is computed from `get_conditional_order` the same way (quantity × orderPrice / triggerPrice per leg, SINGLE / OCO / OTO rule). If any needed price is missing (e.g. a `PROFIT_RATE` leg made in the app), `old` is unknown and the full new notional is counted (conservative).
- Cancel never refunds (same as `cancel_order`).
- The counter floor at 0 and the certain-rejection release rule (`_settle_failure`: paper / Toss 4xx release, timeout / 5xx keep) apply unchanged.
- Audit decisions stay `placed` / `modified` (with `kind: "conditional"`), so `restore_spend` counts them on the memory backend without changes.

## Idempotency

- Create sends `spec.client_order_id` as `clientOrderId`: a same-token retry after an ambiguous failure cannot register twice.
- Modify has no idempotency key; a retry after a modify that did land hits the old (replaced) id and gets a 4xx, which releases the delta. Same known limit as order modify.

## Implementation structure

- **`conditional.py`** (new): input validation, wire payload (`{type, quantity, orderType, expireDate, first, second?}`), leg / group notional, and the tool functions (`preview_conditional_order`, `preview_conditional_modify`, `cancel_conditional_order`, `list_conditional_orders`, `get_conditional_order`, plus `execute_place` / `execute_modify` used by the dispatch).
- **`safety.OrderSpec`**: `kind: str = "order"` and `conditional: dict | None = None` (the SDK `create_conditional_order` kwargs from `build_request`, without `symbol` / `client_order_id` / `confirm_high_value_order`; wire-shaped `first` / `second` dicts). The conditional id to modify reuses `modify_order_id`. `SafetyManager` gets a small constructor for conditional specs (client order id, currency fallback) so specs are only built in `safety.py`.
- **redis serialization**: `_spec_to_dict` / `_spec_from_dict` carry `kind` and `conditional`; missing keys default to an ordinary order, so tokens issued before the upgrade still read.
- **`tools.place_order` / `modify_order`**: after `reserve`, `if spec.kind == CONDITIONAL:` call `conditional.execute_*`, else the existing paper / live branch. The shared prefix (consume → guardrails → reserve) and suffix (`_settle_failure`, commit, audit) are untouched.
- **`server.py`**: register the two reads in `_register_reads`, the three writes in `_register_writes`, all through `mcp_tool`, with `Literal` enums.

## Testing

- Notional: SINGLE / OCO / OTO × LIMIT / MARKET.
- Validation: each `invalid-order-value` / `invalid-order-params` case.
- Preview issues a token only after guardrails (per-order cap, high-value confirm, deny list).
- `place_order` with a conditional token calls `client.create_conditional_order` with `clientOrderId`, reserves the full notional, audits `placed` with `kind`.
- Ambiguous failure keeps the reservation; a 4xx releases it.
- Modify: delta accounting; unknown old notional counts the full new one.
- Cancel: audited, no refund.
- Paper: writes raise live-only; reads return empty / not found.
- Mode registration (read_only: 2 new reads only; paper / live: all 5).
- Redis token roundtrip of a conditional spec, and an old-format token still loads.
- Live verification: OCI runs `read_only`, so only `list` / `get` are checked against the real account. Writes are not exercised live (they would register real conditional orders).
