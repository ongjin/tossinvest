# Currency-aware paper engine — design

**Date:** 2026-06-24
**Status:** approved (design), pending implementation plan
**Scope:** `pytossinvest-mcp` paper engine + config + tools + redis serialization + tests + docs. SDK (`pytossinvest`) untouched.

## Problem

The paper broker tracks cash as a single `Decimal` with **no currency awareness**. On a BUY it does `cost = price * qty` and subtracts from that one pool, regardless of the order's currency. So a USD order ($2,227.84) deducted `2227.84` from a pool seeded as KRW `10,000,000`, leaving `9,997,772.16` — i.e. it treated $2,227.84 as ₩2,227.84. At the real rate (~1 USD = 1,536 KRW) the correct deduction is ~₩3,421,962. The paper cash figure is therefore meaningless once USD trades are involved, and the `cost > cash` buying-power check is effectively unbounded for USD (a few-thousand-dollar notional vs a 10M pool).

This contradicts the project's money-safety rigor and diverges from the real Toss API, which reports holdings per currency (`krw`/`usd` buckets).

## Goals

- Paper cash, buying power, and realized P&L are tracked **per currency**.
- A BUY/SELL only affects the **matching currency bucket**; KRW cash cannot fund a USD order and vice versa.
- No FX conversion anywhere (consistent with the existing guardrail rule that safety amounts never depend on volatile FX).
- Mirror the real API's per-currency shape closely enough to be intuitive.
- Preserve all existing safety invariants (reserve→commit/release, guardrails, `clientOrderId` idempotency) and the public SDK surface.

## Non-goals (YAGNI)

- Real-time market value / unrealized P&L in `holdings()` (needs live prices; paper `holdings()` stays a pure state dump).
- FX conversion / cross-currency funding.
- Arbitrary N-currency configuration UX beyond what JSON dict naturally allows.

## Chosen approach

**Per-currency cash buckets, no FX** (Approach A). Rejected alternatives: single pool + FX conversion (violates "no FX in the money/safety path", non-reproducible balances, an FX call per fill); one-currency-per-instance (cannot hold the user's actual mixed KRW+USD portfolio).

## Data model (`paper.py`)

```python
@dataclass
class Position:
    quantity: Decimal
    avg_price: Decimal
    currency: str            # NEW

@dataclass
class PaperState:
    cash: dict[str, Decimal]            # was: Decimal
    positions: dict[str, Position]      # symbol -> Position (now currency-tagged)
    orders: list[PaperOrder]
    realized_pnl: dict[str, Decimal]    # was: Decimal
    counter: int
```

`PaperOrder` is unchanged (price/quantity are enough; currency is derivable from the position/order context and not required on the order record).

## Behavior (`PaperBroker`)

- `place(*, symbol, side, order_type, fill_price, quantity, currency, client_order_id=None)`:
  - **Idempotency** unchanged: existing order with same `client_order_id` is returned without a second fill.
  - **BUY**: `cost = price*qty`; if `cost > cash.get(currency, 0)` raise `PaperError("insufficient {currency} cash: need {cost}, have {have}")`. Else `cash[currency] -= cost`; create/extend the symbol's `Position` (weighted avg price; `currency` set on create, assumed stable per symbol thereafter).
  - **SELL**: look up position by symbol; if missing or `qty` too large raise `PaperError("insufficient quantity: ...")`. Else `realized_pnl[currency] = realized_pnl.get(currency,0) + (price-avg)*qty`; `cash[currency] = cash.get(currency,0) + price*qty`; reduce/remove position.
- `buying_power(currency: str) -> Decimal` → `cash.get(currency, Decimal("0"))`.
- `sellable_quantity(symbol)` — unchanged.
- `holdings()` returns the new shape:

```json
{
  "cash": {"KRW": "10000000", "USD": "4772"},
  "realizedPnl": {"KRW": "0", "USD": "0"},
  "items": [
    {"symbol": "SOXX", "currency": "USD", "quantity": "1", "averagePurchasePrice": "614.87"}
  ]
}
```

All amounts are strings (JSON/Decimal-safe). This is a **breaking change** to the paper `holdings()` shape; it is our own tool output, and the change makes paper closer to the real API.

## Config (`config.py`)

- `paper_starting_cash: dict[str, Decimal] = {"KRW": Decimal("10000000")}`.
- Env form: `TOSSINVEST_PAPER_STARTING_CASH={"KRW":"10000000","USD":"7000"}` (pydantic-settings JSON-parses dict fields).
- A `mode="before"` validator:
  - If the value is a scalar (str/int/Decimal, e.g. legacy `PAPER_STARTING_CASH=10000000` which JSON-parses to an int), **wrap as `{"KRW": value}`** for backward compatibility.
  - Reject `float`/`bool` whether scalar or as any dict value (`TypeError`), consistent with the money rules. `paper_starting_cash` is removed from the shared `_no_float` validator list and gets this dedicated validator.
- Currencies absent from the dict start at 0 buying power (explicit; USD paper trades fail until USD is seeded).

## Tools layer (`tools.py`)

- `place_order` paper branch: pass `currency=spec.currency` into `app.paper.place(...)`. `spec.currency` is always set by `safety.build_spec` (authoritative from `get_prices`, else `order_currency(symbol)` fallback). **This missing argument is the root of the bug.**
- `get_order_readiness` paper branch: `buying_power(currency)` using the tool's `currency` parameter (mirrors the live path's `get_buying_power(currency)`).
- `get_holdings` paper branch: still `return app.paper.holdings()` (passthrough; only the output shape changes).

## Redis serialization + migration (`redis_stores.py`)

- `_paper_state_to_dict`: serialize `cash` and `realized_pnl` as `{currency: str}`; include `currency` in each serialized position.
- `_paper_state_from_dict`: parse the new shape; **legacy migration shim** so an existing redis `paper` key does not crash boot:
  - if `cash` is not a dict → `{"KRW": to_decimal(cash)}`;
  - if `realized_pnl` is not a dict → `{"KRW": to_decimal(realized_pnl)}`;
  - a position without `currency` → infer `"USD" if symbol.isalpha() else "KRW"`.
- `RedisPaperStore.__init__(starting_cash)` and `MemoryPaperStore.__init__(starting_cash)` now receive the dict; the fresh `PaperState` copies the starting dict (so the seed is not mutated) and seeds `realized_pnl` to `Decimal("0")` for each starting currency (so a fresh `holdings()` shows `{"KRW":"0", ...}` rather than `{}`). `place()` still uses `.get(currency, 0)` so currencies that appear later (e.g. SELL proceeds) are handled too.
- `server.py` wiring is unchanged in shape — it already passes `settings.paper_starting_cash` to both stores.

## Error handling

- Insufficient funds error names the currency: `insufficient USD cash: need 1229.0, have 7000`.
- Currency missing from a bucket is treated as 0 (BUY then fails as insufficient; SELL proceeds credit creates/increments the bucket).
- Unknown side → `PaperError` (unchanged).

## Testing (TDD)

- `test_paper.py`: `place()` requires `currency`; `_broker` seeds a dict. New/updated cases: per-bucket deduction; **USD-insufficient even when KRW is huge**; realized P&L per currency; `holdings()` new shape; `buying_power(currency)` per bucket; idempotency still holds.
- **Regression test** reproducing this bug: start `{"KRW":"10000000","USD":"7000"}`, BUY a USD symbol → KRW bucket unchanged, USD bucket reduced by exactly the USD notional.
- `test_config.py`: JSON-dict parsing; legacy scalar → `{"KRW":...}`; float/bool rejection.
- `test_paper_redis.py` / `test_stores_redis.py` / `test_backend_parity.py`: new serialization round-trip; legacy-format load (migration shim).
- `test_tools_write.py`: paper `place_order` injects currency; holdings/readiness shapes.
- Whole suite (SDK 59 + MCP) stays green; safety invariants untouched.

## Invariants preserved

- `place_order`: consume → `check_guardrails(check_daily=False)` → `reserve` → fill → commit / release. Unchanged.
- Guardrails currency-by-currency (KRW/USD caps) unchanged.
- `clientOrderId` idempotency unchanged.
- SDK public API untouched.

## Docs to self-update (same session, no commit)

- `CLAUDE.md`: `PAPER_STARTING_CASH` now a per-currency JSON dict; paper is currency-aware; update/remove the "통화혼합" 함정 entry.
- `docs/claude/pytossinvest-mcp.md`: paper section (per-currency cash, holdings shape, place currency injection).
- `pytossinvest-mcp/README.md`: config table `PAPER_STARTING_CASH`.
