# Currency-aware Paper Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the paper broker track cash, buying power, and realized P&L per currency so a USD order no longer drains the KRW pool.

**Architecture:** Replace the single `PaperState.cash: Decimal` with `cash: dict[currency → Decimal]` (and `realized_pnl` likewise); tag each `Position` with its currency. BUY/SELL touch only the matching bucket, with no FX conversion. The store constructors normalize a scalar starting-cash to `{"KRW": …}`, which keeps existing tests/wiring working and gives users backward compatibility. The MCP `place_order` passes the order's authoritative `spec.currency` into `paper.place(...)` — the one-line omission that caused the bug.

**Tech Stack:** Python 3.12, uv workspace, pydantic-settings, redis/fakeredis, pytest.

**Spec:** `docs/superpowers/specs/2026-06-24-paper-currency-aware-design.md`

## Global Constraints

- Money/quantity are **string/Decimal everywhere; `float` is rejected with `TypeError`**. Never introduce a float entry path.
- The SDK package `pytossinvest` public API must **not** change. Only `pytossinvest-mcp` is touched.
- Preserve the `place_order` safety invariant: consume → `check_guardrails(check_daily=False)` → `reserve` → fill → `commit` (success) / `release` (failure). Do not bypass it.
- Commit messages: **no AI attribution** of any kind (no `Co-Authored-By`, no "Generated with AI"). Plain conventional-commit messages.
- Work directly on `main` (user directive; no feature branch).
- Both suites stay green at every commit:
  - SDK: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests` (59 tests)
  - MCP: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`
- `holdings()` shape change (cash/realizedPnl become dicts, items gain `currency`) is intentional and only affects paper-mode tool output.

---

### Task 1: Currency-aware broker core (paper.py + redis_stores.py + tools.py)

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/paper.py` (full rewrite below)
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py` (serialization + `RedisPaperStore`)
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/tools.py` (`place_order` paper branch ~229-232; `get_order_readiness` paper branch ~162-167)
- Test: `pytossinvest-mcp/tests/test_paper.py` (full rewrite below)
- Test: `pytossinvest-mcp/tests/test_paper_redis.py` (full rewrite below)

**Interfaces:**
- Produces:
  - `paper._as_cash_dict(v) -> dict[str, Decimal]` — scalar → `{"KRW": Decimal(v)}`, dict → `{str(k): Decimal(v)}`.
  - `Position(quantity: Decimal, avg_price: Decimal, currency: str)`
  - `PaperState(cash: dict[str, Decimal], positions: dict[str, Position], orders: list[PaperOrder], realized_pnl: dict[str, Decimal], counter: int)`
  - `PaperBroker.place(*, symbol, side, order_type, fill_price, quantity, currency: str, client_order_id=None) -> PaperOrder`
  - `PaperBroker.buying_power(currency: str) -> Decimal`
  - `PaperBroker.holdings() -> {"cash": {cur: str}, "realizedPnl": {cur: str}, "items": [{"symbol","currency","quantity","averagePurchasePrice"}]}`
  - `MemoryPaperStore(*, starting_cash=...)` and `RedisPaperStore(client, *, starting_cash, ...)` accept scalar **or** dict.
- Consumes: `spec.currency` (always set by `safety.build_spec`) inside `tools.place_order`.

- [ ] **Step 1: Write the failing regression test** (the exact bug) — add to `pytossinvest-mcp/tests/test_paper.py`:

```python
def test_currency_buckets_are_isolated():
    # the bug this whole change fixes: a USD buy must NOT dent the KRW bucket
    from pytossinvest_mcp.paper import PaperBroker, MemoryPaperStore
    b = PaperBroker(MemoryPaperStore(starting_cash={"KRW": "10000000", "USD": "7000"}))
    b.place(symbol="SOXX", side="BUY", order_type="MARKET",
            fill_price="614.87", quantity="1", currency="USD")
    h = b.holdings()
    assert h["cash"]["KRW"] == "10000000"          # untouched
    assert h["cash"]["USD"] == "6385.13"           # 7000 - 614.87
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_paper.py::test_currency_buckets_are_isolated -v`
Expected: FAIL — `TypeError: place() got an unexpected keyword argument 'currency'` (and `MemoryPaperStore` rejects a dict).

- [ ] **Step 3: Rewrite `pytossinvest-mcp/src/pytossinvest_mcp/paper.py`** to this complete file:

```python
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

from pytossinvest.money import to_decimal


class PaperError(Exception):
    """Paper-broker rule violation (insufficient cash/quantity, bad side)."""


def _as_cash_dict(v) -> dict[str, Decimal]:
    """Normalize a starting-cash arg to {currency: Decimal}. A scalar -> {'KRW': scalar}."""
    if isinstance(v, dict):
        return {str(k): to_decimal(x) for k, x in v.items()}
    return {"KRW": to_decimal(v)}


@dataclass
class Position:
    quantity: Decimal
    avg_price: Decimal
    currency: str


@dataclass
class PaperOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    status: str
    client_order_id: "str | None" = None


@dataclass
class PaperState:
    cash: dict[str, Decimal]
    positions: dict[str, Position]
    orders: list[PaperOrder]
    realized_pnl: dict[str, Decimal]
    counter: int


class PaperStore(Protocol):
    def lock(self): ...
    def load(self) -> PaperState: ...
    def save(self, state: PaperState) -> None: ...


class MemoryPaperStore:
    def __init__(self, *, starting_cash: "str | int | Decimal | dict" = "10000000"):
        cash = _as_cash_dict(starting_cash)
        self._state = PaperState(
            cash=cash, positions={}, orders=[],
            realized_pnl={cur: Decimal("0") for cur in cash}, counter=0,
        )

    def lock(self):
        return contextlib.nullcontext()

    def load(self) -> PaperState:
        return self._state

    def save(self, state: PaperState) -> None:
        self._state = state


class PaperBroker:
    def __init__(self, store: PaperStore, *, next_id: "Callable[[], str] | None" = None):
        self._store = store
        self._next_id = next_id

    def _make_id(self, state: PaperState) -> str:
        if self._next_id is not None:
            return self._next_id()
        state.counter += 1
        return f"paper-{state.counter}"

    def buying_power(self, currency: str) -> Decimal:
        return self._store.load().cash.get(currency, Decimal("0"))

    def sellable_quantity(self, symbol: str) -> Decimal:
        pos = self._store.load().positions.get(symbol)
        return pos.quantity if pos else Decimal("0")

    def place(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        fill_price: "str | int | Decimal",
        quantity: "str | int | Decimal",
        currency: str,
        client_order_id: "str | None" = None,
    ) -> PaperOrder:
        with self._store.lock():
            state = self._store.load()
            if client_order_id is not None:
                existing = next((o for o in state.orders
                                 if o.client_order_id == client_order_id), None)
                if existing is not None:
                    return existing  # idempotent: no second fill
            price = to_decimal(fill_price)
            qty = to_decimal(quantity)
            if side == "BUY":
                cost = price * qty
                have = state.cash.get(currency, Decimal("0"))
                if cost > have:
                    raise PaperError(f"insufficient {currency} cash: need {cost}, have {have}")
                state.cash[currency] = have - cost
                pos = state.positions.get(symbol)
                if pos:
                    total = pos.quantity + qty
                    pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total
                    pos.quantity = total
                else:
                    state.positions[symbol] = Position(
                        quantity=qty, avg_price=price, currency=currency)
            elif side == "SELL":
                pos = state.positions.get(symbol)
                if pos is None or pos.quantity < qty:
                    have = pos.quantity if pos else Decimal("0")
                    raise PaperError(f"insufficient quantity: need {qty}, have {have}")
                cur = pos.currency  # the position's currency is authoritative for the bucket
                state.realized_pnl[cur] = (
                    state.realized_pnl.get(cur, Decimal("0")) + (price - pos.avg_price) * qty)
                state.cash[cur] = state.cash.get(cur, Decimal("0")) + price * qty
                pos.quantity -= qty
                if pos.quantity == 0:
                    del state.positions[symbol]
            else:
                raise PaperError(f"unknown side: {side}")

            order = PaperOrder(
                order_id=self._make_id(state), symbol=symbol, side=side,
                order_type=order_type, quantity=qty, price=price, status="FILLED",
                client_order_id=client_order_id,
            )
            state.orders.append(order)
            self._store.save(state)
            return order

    def get_order(self, order_id: str) -> "PaperOrder | None":
        return next((o for o in self._store.load().orders if o.order_id == order_id), None)

    def list_orders(self) -> list[PaperOrder]:
        return list(self._store.load().orders)

    def holdings(self) -> dict:
        state = self._store.load()
        return {
            "cash": {cur: str(amt) for cur, amt in state.cash.items()},
            "realizedPnl": {cur: str(amt) for cur, amt in state.realized_pnl.items()},
            "items": [
                {"symbol": s, "currency": p.currency, "quantity": str(p.quantity),
                 "averagePurchasePrice": str(p.avg_price)}
                for s, p in state.positions.items()
            ],
        }
```

- [ ] **Step 4: Update `pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py`**

Change the import at the top from:

```python
from .paper import PaperState, Position, PaperOrder
```

to:

```python
from .paper import PaperState, Position, PaperOrder, _as_cash_dict
```

Replace `_paper_state_to_dict` and `_paper_state_from_dict` (currently lines ~126-163) with:

```python
def _paper_state_to_dict(state: PaperState) -> dict:
    return {
        "cash": {cur: str(amt) for cur, amt in state.cash.items()},
        "realized_pnl": {cur: str(amt) for cur, amt in state.realized_pnl.items()},
        "counter": state.counter,
        "positions": {
            s: {"quantity": str(p.quantity), "avg_price": str(p.avg_price),
                "currency": p.currency}
            for s, p in state.positions.items()
        },
        "orders": [
            {"order_id": o.order_id, "symbol": o.symbol, "side": o.side,
             "order_type": o.order_type, "quantity": str(o.quantity),
             "price": str(o.price), "status": o.status,
             "client_order_id": o.client_order_id}
            for o in state.orders
        ],
    }


def _paper_state_from_dict(d: dict) -> PaperState:
    raw_cash = d["cash"]
    cash = ({c: to_decimal(v) for c, v in raw_cash.items()} if isinstance(raw_cash, dict)
            else {"KRW": to_decimal(raw_cash)})  # legacy scalar -> KRW
    raw_pnl = d.get("realized_pnl", {})
    realized = ({c: to_decimal(v) for c, v in raw_pnl.items()} if isinstance(raw_pnl, dict)
                else {"KRW": to_decimal(raw_pnl)})
    positions = {}
    for s, p in d["positions"].items():
        cur = p.get("currency") or ("USD" if s.isalpha() else "KRW")  # legacy infer
        positions[s] = Position(quantity=to_decimal(p["quantity"]),
                                avg_price=to_decimal(p["avg_price"]), currency=cur)
    return PaperState(
        cash=cash,
        realized_pnl=realized,
        counter=d["counter"],
        positions=positions,
        orders=[
            PaperOrder(
                order_id=o["order_id"], symbol=o["symbol"], side=o["side"],
                order_type=o["order_type"], quantity=to_decimal(o["quantity"]),
                price=to_decimal(o["price"]), status=o["status"],
                client_order_id=o["client_order_id"],
            )
            for o in d["orders"]
        ],
    )
```

Replace `RedisPaperStore.__init__` and `load` (currently lines ~167-182) with:

```python
    def __init__(self, client, *, starting_cash, key: str = "paper", lock_timeout: float = 5.0):
        self._r = client
        self._key = key
        self._starting = _as_cash_dict(starting_cash)
        self._lock_timeout = lock_timeout

    def lock(self):
        return self._r.lock(f"lock:{self._key}", timeout=self._lock_timeout,
                            blocking_timeout=self._lock_timeout)

    def load(self) -> PaperState:
        raw = self._r.get(self._key)
        if raw is None:
            return PaperState(cash=dict(self._starting), positions={}, orders=[],
                              realized_pnl={cur: Decimal("0") for cur in self._starting},
                              counter=0)
        return _paper_state_from_dict(json.loads(raw))
```

(`save` is unchanged.)

- [ ] **Step 5: Update `pytossinvest-mcp/src/pytossinvest_mcp/tools.py`**

In `place_order`'s paper branch, add `currency=spec.currency` to the `app.paper.place(...)` call:

```python
            order = app.paper.place(
                symbol=spec.symbol, side=spec.side, order_type=spec.order_type,
                fill_price=fill_price, quantity=qty, currency=spec.currency,
                client_order_id=spec.client_order_id,
            )
```

In `get_order_readiness`'s paper branch, pass the currency to `buying_power`:

```python
    if app.use_paper:
        return {
            "buyingPower": str(app.paper.buying_power(currency)),
            "sellableQuantity": str(app.paper.sellable_quantity(symbol)),
            "commissions": [],
        }
```

- [ ] **Step 6: Replace `pytossinvest-mcp/tests/test_paper.py`** with the full file:

```python
from decimal import Decimal

import pytest

from pytossinvest_mcp.paper import PaperBroker, MemoryPaperStore, PaperError


def _broker(cash="10000000", next_id=None):
    return PaperBroker(MemoryPaperStore(starting_cash=cash), next_id=next_id)


def test_starts_with_configured_cash():
    b = _broker(cash="1000000")
    assert b.buying_power("KRW") == Decimal("1000000")
    assert b.holdings()["items"] == []


def test_scalar_starting_cash_wraps_to_krw():
    b = _broker(cash="1000000")
    assert b.holdings()["cash"] == {"KRW": "1000000"}
    assert b.holdings()["realizedPnl"] == {"KRW": "0"}


def test_buy_fills_and_reduces_cash():
    b = _broker(cash="1000000")
    order = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                    fill_price="70000", quantity="10", currency="KRW")
    assert order.status == "FILLED"
    assert order.order_id == "paper-1"
    assert b.buying_power("KRW") == Decimal("300000")  # 1,000,000 - 70,000*10
    assert b.sellable_quantity("005930") == Decimal("10")


def test_buy_insufficient_cash_rejected():
    b = _broker(cash="100000")
    with pytest.raises(PaperError, match="insufficient KRW cash"):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="10", currency="KRW")


def test_currency_buckets_are_isolated():
    # the bug this whole change fixes: a USD buy must NOT dent the KRW bucket
    b = PaperBroker(MemoryPaperStore(starting_cash={"KRW": "10000000", "USD": "7000"}))
    b.place(symbol="SOXX", side="BUY", order_type="MARKET",
            fill_price="614.87", quantity="1", currency="USD")
    h = b.holdings()
    assert h["cash"]["KRW"] == "10000000"          # untouched
    assert h["cash"]["USD"] == "6385.13"           # 7000 - 614.87
    assert b.buying_power("KRW") == Decimal("10000000")
    assert b.buying_power("USD") == Decimal("6385.13")


def test_usd_insufficient_even_when_krw_is_huge():
    b = PaperBroker(MemoryPaperStore(starting_cash={"KRW": "10000000", "USD": "500"}))
    with pytest.raises(PaperError, match="insufficient USD cash"):
        b.place(symbol="SOXX", side="BUY", order_type="MARKET",
                fill_price="614.87", quantity="1", currency="USD")


def test_buy_then_sell_realizes_pnl_per_currency():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="65000", quantity="10", currency="KRW")
    b.place(symbol="005930", side="SELL", order_type="LIMIT",
            fill_price="70000", quantity="10", currency="KRW")
    h = b.holdings()
    assert h["realizedPnl"]["KRW"] == "50000"  # (70000-65000)*10
    assert b.sellable_quantity("005930") == Decimal("0")


def test_sell_more_than_held_rejected():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="65000", quantity="5", currency="KRW")
    with pytest.raises(PaperError, match="insufficient quantity"):
        b.place(symbol="005930", side="SELL", order_type="LIMIT",
                fill_price="70000", quantity="10", currency="KRW")


def test_average_price_updates_on_add():
    b = _broker(cash="10000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="60000", quantity="10", currency="KRW")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="80000", quantity="10", currency="KRW")
    item = b.holdings()["items"][0]
    assert item["quantity"] == "20"
    assert item["averagePurchasePrice"] == "70000"
    assert item["currency"] == "KRW"


def test_holdings_and_orders_are_strings():
    b = _broker(cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="10", currency="KRW", client_order_id="cli-1")
    h = b.holdings()
    assert h["cash"]["KRW"] == "300000"
    assert h["items"][0] == {"symbol": "005930", "currency": "KRW",
                             "quantity": "10", "averagePurchasePrice": "70000"}
    listed = b.list_orders()
    assert listed[0].client_order_id == "cli-1"
    assert b.get_order("paper-1").symbol == "005930"
    assert b.get_order("nope") is None


def test_place_is_idempotent_by_client_order_id():
    b = _broker(cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")
    assert o2.order_id == o1.order_id          # same order returned, not a second fill
    assert len(b.list_orders()) == 1
    assert b.holdings()["items"][0]["quantity"] == "1"  # only one fill applied
```

- [ ] **Step 7: Replace `pytossinvest-mcp/tests/test_paper_redis.py`** with the full file:

```python
from decimal import Decimal

import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.paper import PaperBroker, Position, PaperError
from pytossinvest_mcp.redis_stores import (
    RedisPaperStore, _paper_state_to_dict, _paper_state_from_dict,
)


@pytest.fixture
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _broker(r, cash="1000000"):
    return PaperBroker(RedisPaperStore(r, starting_cash=cash))


def test_buy_then_holdings_decimal_exact(r):
    b = _broker(r, cash="1000")
    b.place(symbol="AAPL", side="BUY", order_type="LIMIT",
            fill_price="0.1", quantity="3", currency="KRW", client_order_id="c1")
    h = b.holdings()
    assert h["cash"]["KRW"] == "999.7"                # 1000 - 0.3, exact (not 999.6999...)
    assert h["items"][0]["averagePurchasePrice"] == "0.1"
    assert h["items"][0]["quantity"] == "3"


def test_sell_realizes_pnl(r):
    b = _broker(r, cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="2", currency="KRW", client_order_id="c1")
    b.place(symbol="005930", side="SELL", order_type="LIMIT",
            fill_price="80000", quantity="1", currency="KRW", client_order_id="c2")
    h = b.holdings()
    assert h["realizedPnl"]["KRW"] == "10000"         # (80000-70000)*1
    assert h["items"][0]["quantity"] == "1"


def test_insufficient_cash_raises(r):
    b = _broker(r, cash="100")
    with pytest.raises(PaperError, match="insufficient KRW cash"):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")


def test_usd_bucket_isolated_over_redis(r):
    b = PaperBroker(RedisPaperStore(r, starting_cash={"KRW": "10000000", "USD": "7000"}))
    b.place(symbol="SOXX", side="BUY", order_type="MARKET",
            fill_price="614.87", quantity="1", currency="USD", client_order_id="c1")
    h = b.holdings()
    assert h["cash"]["KRW"] == "10000000"
    assert h["cash"]["USD"] == "6385.13"
    assert h["items"][0]["currency"] == "USD"


def test_place_idempotent_by_client_order_id(r):
    b = _broker(r, cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="dup")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="dup")
    assert o2.order_id == o1.order_id
    assert len(b.list_orders()) == 1


def test_two_brokers_share_state(r):
    a = _broker(r, cash="1000000")
    b = PaperBroker(RedisPaperStore(r, starting_cash="1000000"))  # same fakeredis
    a.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="1", currency="KRW", client_order_id="c1")
    # instance b sees instance a's fill
    assert b.sellable_quantity("005930") == Decimal("1")
    assert b.holdings()["items"][0]["quantity"] == "1"


def test_concurrent_same_coid_single_fill(r):
    a = _broker(r, cash="1000000")
    b = PaperBroker(RedisPaperStore(r, starting_cash="1000000"))
    o1 = a.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="same")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", currency="KRW", client_order_id="same")
    assert o1.order_id == o2.order_id           # dedup across instances
    assert len(a.list_orders()) == 1            # one fill total


def test_legacy_scalar_state_migrates_on_load(r):
    # an old redis paper key (single scalar cash, no per-position currency) must load, not crash
    legacy = {
        "cash": "500000",
        "realized_pnl": "0",
        "counter": 1,
        "positions": {"005930": {"quantity": "2", "avg_price": "70000"}},
        "orders": [],
    }
    import json
    r.set("paper", json.dumps(legacy))
    state = _paper_state_from_dict(json.loads(r.get("paper")))
    assert state.cash == {"KRW": Decimal("500000")}
    assert state.realized_pnl == {"KRW": Decimal("0")}
    assert state.positions["005930"].currency == "KRW"  # numeric symbol -> KRW


def test_round_trip_serialization(r):
    state = PaperState_with_two_currencies()
    d = _paper_state_to_dict(state)
    back = _paper_state_from_dict(d)
    assert back.cash == {"KRW": Decimal("100"), "USD": Decimal("50")}
    assert back.positions["SOXX"].currency == "USD"


def PaperState_with_two_currencies():
    from pytossinvest_mcp.paper import PaperState
    return PaperState(
        cash={"KRW": Decimal("100"), "USD": Decimal("50")},
        positions={"SOXX": Position(quantity=Decimal("1"), avg_price=Decimal("10"), currency="USD")},
        orders=[],
        realized_pnl={"KRW": Decimal("0"), "USD": Decimal("0")},
        counter=0,
    )
```

- [ ] **Step 8: Run the full MCP suite and the SDK suite, verify green**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`
Expected: PASS (all MCP tests; `test_paper.py`, `test_paper_redis.py`, `test_tools_write.py`, `test_backend_parity.py` all green).
Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests`
Expected: PASS (59; SDK untouched).

- [ ] **Step 9: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/paper.py \
        pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py \
        pytossinvest-mcp/src/pytossinvest_mcp/tools.py \
        pytossinvest-mcp/tests/test_paper.py \
        pytossinvest-mcp/tests/test_paper_redis.py
git commit -m "feat(mcp): per-currency paper cash buckets (no FX)

Paper cash/realized-pnl are now {currency: Decimal} and positions carry a
currency tag; BUY/SELL touch only the matching bucket. place_order passes
the order's authoritative currency to the broker. Stores normalize a scalar
starting-cash to {KRW: ...} and migrate legacy redis state. Fixes USD orders
draining the KRW pool."
```

---

### Task 2: Per-currency starting cash config

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/config.py` (field ~35; validator ~58-66)
- Test: `pytossinvest-mcp/tests/test_config.py` (append cases)

**Interfaces:**
- Consumes: store constructors from Task 1 (already accept scalar **or** dict).
- Produces: `Settings.paper_starting_cash: dict[str, Decimal]`, default `{"KRW": Decimal("10000000")}`; env `TOSSINVEST_PAPER_STARTING_CASH` accepts a JSON dict or a legacy scalar.

- [ ] **Step 1: Write the failing tests** — append to `pytossinvest-mcp/tests/test_config.py`:

```python
def test_paper_starting_cash_default():
    assert _settings().paper_starting_cash == {"KRW": Decimal("10000000")}


def test_paper_starting_cash_dict():
    s = _settings(paper_starting_cash={"KRW": "10000000", "USD": "7000"})
    assert s.paper_starting_cash == {"KRW": Decimal("10000000"), "USD": Decimal("7000")}


def test_paper_starting_cash_scalar_wraps_krw():
    s = _settings(paper_starting_cash="5000000")
    assert s.paper_starting_cash == {"KRW": Decimal("5000000")}


def test_paper_starting_cash_rejects_float_value():
    with pytest.raises(Exception):
        _settings(paper_starting_cash={"USD": 7000.5})


def test_paper_starting_cash_rejects_float_scalar():
    with pytest.raises(Exception):
        _settings(paper_starting_cash=1000.5)


def test_paper_starting_cash_from_env_json(monkeypatch):
    monkeypatch.setenv("TOSSINVEST_PAPER_STARTING_CASH", '{"KRW":"10000000","USD":"7000"}')
    s = Settings(_env_file=None)
    assert s.paper_starting_cash == {"KRW": Decimal("10000000"), "USD": Decimal("7000")}


def test_paper_starting_cash_legacy_scalar_env(monkeypatch):
    monkeypatch.setenv("TOSSINVEST_PAPER_STARTING_CASH", "5000000")
    s = Settings(_env_file=None)
    assert s.paper_starting_cash == {"KRW": Decimal("5000000")}
```

- [ ] **Step 2: Run, verify they fail**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_config.py -v -k paper_starting_cash`
Expected: FAIL — `paper_starting_cash` is still a scalar `Decimal`, so the dict/default assertions fail.

- [ ] **Step 3: Change the field** in `config.py`. Replace:

```python
    # paper engine
    paper_starting_cash: Decimal = Decimal("10000000")
```

with:

```python
    # paper engine (per-currency cash buckets; scalar is wrapped as {"KRW": ...})
    paper_starting_cash: dict[str, Decimal] = {"KRW": Decimal("10000000")}
```

- [ ] **Step 4: Remove `paper_starting_cash` from the shared `_no_float` validator and add a dedicated one.** Change the `@field_validator(...)` decorator list (currently lines ~58-61) from:

```python
    @field_validator(
        "max_order_amount", "daily_order_limit", "paper_starting_cash",
        "max_order_amount_usd", "daily_order_limit_usd", mode="before",
    )
    @classmethod
    def _no_float(cls, v):
        if isinstance(v, float):
            raise TypeError("money config must be a string or int, never float")
        return v
```

to (drop `paper_starting_cash` from the list, add a new validator below it):

```python
    @field_validator(
        "max_order_amount", "daily_order_limit",
        "max_order_amount_usd", "daily_order_limit_usd", mode="before",
    )
    @classmethod
    def _no_float(cls, v):
        if isinstance(v, float):
            raise TypeError("money config must be a string or int, never float")
        return v

    @field_validator("paper_starting_cash", mode="before")
    @classmethod
    def _paper_cash_dict(cls, v):
        def _reject_float(x):
            if isinstance(x, bool) or isinstance(x, float):
                raise TypeError(
                    "paper_starting_cash must be string/int per currency, never float/bool")
        if isinstance(v, dict):
            for x in v.values():
                _reject_float(x)
            return v
        _reject_float(v)
        return {"KRW": v}  # legacy scalar -> KRW bucket
```

- [ ] **Step 5: Run the config tests, verify green**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_config.py -v`
Expected: PASS (existing + new paper_starting_cash cases).

- [ ] **Step 6: Run the full MCP suite, verify still green**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/config.py \
        pytossinvest-mcp/tests/test_config.py
git commit -m "feat(mcp): PAPER_STARTING_CASH as per-currency JSON dict

TOSSINVEST_PAPER_STARTING_CASH now accepts {\"KRW\":..,\"USD\":..}; a legacy
scalar is wrapped as {KRW: ...}. Floats/bools rejected per the money rules."
```

---

### Task 3: Documentation self-update

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/claude/pytossinvest-mcp.md`
- Modify: `pytossinvest-mcp/README.md`

**Interfaces:** none (docs only). No tests.

- [ ] **Step 1: Update `pytossinvest-mcp/README.md` config table.** Replace the `PAPER_STARTING_CASH` row (currently `| PAPER_STARTING_CASH | 10000000 | paper 포트폴리오 시작 현금 |`) with:

```markdown
| `PAPER_STARTING_CASH` | `{"KRW":"10000000"}` | paper 시작 현금 (**통화별 JSON dict**). 예 `{"KRW":"10000000","USD":"7000"}`. 스칼라(`10000000`)는 `{"KRW": …}` 로 래핑 |
```

Also, in the paper section, note that paper cash/buying-power/realized-pnl are **per currency** (KRW/USD 분리, FX 환산 없음) and that `get_holdings` (paper) returns `cash`/`realizedPnl` as `{통화: 문자열}` and items carry `currency`.

- [ ] **Step 2: Update `docs/claude/pytossinvest-mcp.md`** paper section: paper engine is currency-aware — `PaperState.cash`/`realized_pnl` are `{currency: Decimal}`, `Position` has `currency`, `place()` requires `currency` (injected from `spec.currency` in `tools.place_order`), `buying_power(currency)`, `holdings()` returns per-currency cash. Redis serialization carries currency and migrates legacy scalar state (scalar cash → KRW, position currency inferred by `isalpha()`).

- [ ] **Step 3: Update `CLAUDE.md`.** In the MCP conventions/함정 area, replace the previous single-pool wording with: paper 현금은 **통화별 버킷**(KRW/USD 분리, FX 환산 없음) — `place` 는 `spec.currency` 로 해당 버킷만 차감/입금, `PAPER_STARTING_CASH` 는 통화별 JSON dict(스칼라는 `{"KRW":…}` 래핑). `get_holdings`(paper) 의 `cash`/`realizedPnl` 은 통화별 dict. (이전의 "현금풀 통화 무구분" 설명은 제거/수정.)

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/claude/pytossinvest-mcp.md pytossinvest-mcp/README.md
git commit -m "docs: per-currency paper cash (config, holdings shape, behavior)"
```

---

## Self-Review

**1. Spec coverage:**
- Data model (Position.currency, PaperState dicts) → Task 1 Step 3. ✓
- Behavior (per-bucket BUY/SELL, buying_power(currency), insufficient-by-currency, holdings shape) → Task 1 Steps 3, 6, 7. ✓
- Config (dict, scalar-wrap, float/bool reject, JSON env) → Task 2. ✓
- Tools currency injection + readiness → Task 1 Step 5; covered indirectly by `test_tools_write.py`/`test_backend_parity.py` staying green (Step 8) and directly by the broker regression test. ✓
- Redis serialization + legacy migration → Task 1 Step 4, tested in Task 1 Step 7 (`test_legacy_scalar_state_migrates_on_load`, `test_round_trip_serialization`). ✓
- Invariants preserved (reserve→commit/release, idempotency, SDK untouched) → only the `paper.place` call and readiness changed in `tools.py`; idempotency covered by `test_place_is_idempotent_*`. ✓
- Docs self-update → Task 3. ✓

**2. Placeholder scan:** No TBD/TODO; every code/test step shows full content. ✓

**3. Type consistency:** `place(*, currency)` defined in Task 1 Step 3 and used with `currency=` in Task 1 Steps 5/6/7. `buying_power(currency)` defined Step 3, used Step 5/6. `_as_cash_dict` defined in `paper.py` Step 3, imported in `redis_stores.py` Step 4. `holdings()` cash/realizedPnl dict shape asserted consistently in test_paper/test_paper_redis. Config `paper_starting_cash: dict[str, Decimal]` consumed by stores that already accept dict. ✓

**Note for executor:** `test_round_trip_serialization` references a `PaperState` import via the helper `PaperState_with_two_currencies` — it imports `PaperState` inside the helper; keep that import. Commit messages must contain **no AI attribution** (Global Constraints).
