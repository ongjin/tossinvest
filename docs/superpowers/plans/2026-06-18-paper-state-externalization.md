# Paper-State Externalization Implementation Plan (Phase 1, Plan 2/3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the `PaperBroker` state (cash, positions, orders, realized PnL, id counter) behind a `PaperStore` seam with a `memory` (default) and a `redis` backend, so paper trading works correctly across HA instances and survives restarts. This removes the Plan-1 interim limitation ("paper state is per-instance under the redis backend").

**Architecture:** `PaperBroker` keeps all its fill math (cost/avg-price/PnL) but delegates state storage to an injected `PaperStore`. The memory impl holds the live in-process `PaperState`; the redis impl serializes `PaperState` to a single JSON key with money as decimal strings, mutated under a redis-py `Lock` (the same lock pattern as the spend store). `place()` runs inside the lock and is idempotent by `clientOrderId` (a repeated id returns the existing order instead of re-filling).

**Tech Stack:** Python 3.12, `redis` (optional extra), `fakeredis[lua]` (dev), `pytest`. Builds on Plan 1 (store seam, redis stores, backend selection).

## Global Constraints

- **Money/quantity are NEVER float** — strings/`Decimal` end-to-end. Paper state in Redis is JSON with **all money fields as decimal strings** (cash, realized_pnl, `Position.quantity`/`avg_price`, `PaperOrder.quantity`/`price`). No float ingress; no Redis numeric ops on money.
- **SDK public API must not change** — `pytossinvest-mcp` only; `pytossinvest` untouched.
- **PaperBroker public method signatures stay stable** — `buying_power()`, `sellable_quantity(symbol)`, `place(*, symbol, side, order_type, fill_price, quantity, client_order_id=None)`, `get_order(order_id)`, `list_orders()`, `holdings()` keep their signatures so `tools.py` needs NO change. Only the **constructor** changes (takes a `PaperStore` instead of `starting_cash`).
- **paper place idempotency** — within the lock, a repeated `client_order_id` returns the existing `PaperOrder` (no second fill). Mirrors the live path's Toss-side clientOrderId dedup.
- **Tests: zero network, no live keys** — redis paper tested via `fakeredis[lua]` (already a dev dep from Plan 1).
- **Test imports** — `from conftest import ...` (never `from tests.conftest`).
- **No AI-authorship markers** anywhere.
- **Commits happen on the feature branch per-task**; no push/merge without the user's request.
- **Commands run from repo root** `/Users/cyj/workspace/personal/toss`. MCP tests: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`.
- **Branch**: do this on a fresh `feat/paper-state-externalization` branch off `main` (main already has Plan 1 merged).

## File Structure

- `pytossinvest-mcp/src/pytossinvest_mcp/paper.py` — gains `PaperState`, `PaperStore` (Protocol), `MemoryPaperStore`; `PaperBroker` refactored to take a `PaperStore`. (Keeps `Position`, `PaperOrder`, `PaperError`.)
- `pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py` — gains `RedisPaperStore` (+ `_paper_state_to_dict`/`_paper_state_from_dict`).
- `pytossinvest-mcp/src/pytossinvest_mcp/server.py` — `_build_stores` (or `build_app_context`) selects the paper store by backend and constructs `PaperBroker` with it.
- `pytossinvest-mcp/tests/conftest.py` — `make_app` builds the paper store per `backend`.
- Tests: `test_paper.py` (adapt to new constructor), `test_paper_redis.py` (new), `test_backend_parity.py` (extend with a paper scenario).

---

### Task 1: `paper.py` — PaperStore seam + memory impl + PaperBroker refactor

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/paper.py`
- Test: `pytossinvest-mcp/tests/test_paper.py` (adapt existing)

**Interfaces:**
- Produces:
  - `@dataclass PaperState`: `cash: Decimal`, `positions: dict[str, Position]`, `orders: list[PaperOrder]`, `realized_pnl: Decimal`, `counter: int`.
  - `class PaperStore(Protocol)`: `lock() -> ContextManager`, `load() -> PaperState`, `save(state: PaperState) -> None`.
  - `class MemoryPaperStore` (implements `PaperStore`): `__init__(self, *, starting_cash)`.
  - `PaperBroker.__init__(self, store: PaperStore, *, next_id: Callable[[], str] | None = None)`. All existing public methods preserved; mutating `place()` runs inside `store.lock()` and is idempotent by `client_order_id`.

- [ ] **Step 1: Adapt the existing paper tests + add the dedup test (write them, expect red)**

`test_paper.py` currently constructs `PaperBroker(starting_cash="...", next_id=...)`. Change every construction to:
```python
from pytossinvest_mcp.paper import PaperBroker, MemoryPaperStore, PaperError

def _broker(cash="10000000", next_id=None):
    return PaperBroker(MemoryPaperStore(starting_cash=cash), next_id=next_id)
```
and replace direct `PaperBroker(...)` calls with `_broker(...)`. Keep all existing assertions (buy reduces cash, avg-price on add, sell realizes PnL, insufficient-cash/quantity raise `PaperError`, holdings shape). Add:
```python
def test_place_is_idempotent_by_client_order_id():
    b = _broker(cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="c1")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="c1")
    assert o2.order_id == o1.order_id          # same order returned, not a second fill
    assert len(b.list_orders()) == 1
    h = b.holdings()
    assert h["items"][0]["quantity"] == "1"    # only one fill applied
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_paper.py -v`
Expected: FAIL — `cannot import name 'MemoryPaperStore'` / `PaperBroker.__init__` signature mismatch.

- [ ] **Step 3: Refactor `paper.py`**

Add imports at top: `import contextlib`, `from typing import Callable, Protocol`. Add after `PaperOrder`:

```python
@dataclass
class PaperState:
    cash: Decimal
    positions: dict[str, Position]
    orders: list[PaperOrder]
    realized_pnl: Decimal
    counter: int


class PaperStore(Protocol):
    def lock(self): ...
    def load(self) -> PaperState: ...
    def save(self, state: PaperState) -> None: ...


class MemoryPaperStore:
    def __init__(self, *, starting_cash: "str | int | Decimal" = "10000000"):
        self._state = PaperState(
            cash=to_decimal(starting_cash), positions={}, orders=[],
            realized_pnl=Decimal("0"), counter=0,
        )

    def lock(self):
        return contextlib.nullcontext()

    def load(self) -> PaperState:
        return self._state

    def save(self, state: PaperState) -> None:
        self._state = state
```

Replace `PaperBroker` with the store-backed version (preserve all fill math verbatim, just operate on `state`):

```python
class PaperBroker:
    def __init__(self, store: PaperStore, *, next_id: "Callable[[], str] | None" = None):
        self._store = store
        self._next_id = next_id

    def _make_id(self, state: PaperState) -> str:
        if self._next_id is not None:
            return self._next_id()
        state.counter += 1
        return f"paper-{state.counter}"

    def buying_power(self) -> Decimal:
        return self._store.load().cash

    def sellable_quantity(self, symbol: str) -> Decimal:
        pos = self._store.load().positions.get(symbol)
        return pos.quantity if pos else Decimal("0")

    def place(self, *, symbol, side, order_type, fill_price, quantity,
              client_order_id: "str | None" = None) -> PaperOrder:
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
                if cost > state.cash:
                    raise PaperError(f"insufficient cash: need {cost}, have {state.cash}")
                state.cash -= cost
                pos = state.positions.get(symbol)
                if pos:
                    total = pos.quantity + qty
                    pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total
                    pos.quantity = total
                else:
                    state.positions[symbol] = Position(quantity=qty, avg_price=price)
            elif side == "SELL":
                pos = state.positions.get(symbol)
                if pos is None or pos.quantity < qty:
                    have = pos.quantity if pos else Decimal("0")
                    raise PaperError(f"insufficient quantity: need {qty}, have {have}")
                state.realized_pnl += (price - pos.avg_price) * qty
                state.cash += price * qty
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
            "cash": str(state.cash),
            "realizedPnl": str(state.realized_pnl),
            "items": [
                {"symbol": s, "quantity": str(p.quantity),
                 "averagePurchasePrice": str(p.avg_price)}
                for s, p in state.positions.items()
            ],
        }
```

Remove the old `cash`/`positions`/`orders`/`realized_pnl`/`_counter`/`_default_id` attributes from `PaperBroker` (now in state/store).

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_paper.py -v`
Expected: PASS (existing paper tests + new idempotency test). The BROADER suite is expected to be red (conftest/server still use the old constructor — Task 2 fixes that). Do NOT touch conftest/server here.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/paper.py pytossinvest-mcp/tests/test_paper.py
git commit -m "refactor(mcp): paper broker over PaperStore seam (memory) + clientOrderId dedup"
```

---

### Task 2: Wire memory paper store (conftest + server) → full suite green

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/server.py`
- Modify: `pytossinvest-mcp/tests/conftest.py`

**Interfaces:**
- Consumes: `MemoryPaperStore`, refactored `PaperBroker`.
- Produces: `build_app_context` and `conftest.make_app` construct `PaperBroker(MemoryPaperStore(starting_cash=settings.paper_starting_cash), ...)`. (Redis paper selection comes in Task 4.)

- [ ] **Step 1: Run the full suite to confirm the Task-1 breakage**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q`
Expected: FAIL in tests that build the app (conftest/server still call `PaperBroker(starting_cash=...)`).

- [ ] **Step 2: Update `server.py` `build_app_context`**

Change the paper construction line from:
```python
    paper = PaperBroker(starting_cash=settings.paper_starting_cash)
```
to:
```python
    from .paper import MemoryPaperStore
    paper = PaperBroker(MemoryPaperStore(starting_cash=settings.paper_starting_cash))
```
(Add `MemoryPaperStore` to the existing `from .paper import PaperBroker` line instead of the inline import if cleaner.)

- [ ] **Step 3: Update `conftest.make_app`**

Change:
```python
    paper = PaperBroker(starting_cash=settings.paper_starting_cash, next_id=_counter("paper"))
```
to:
```python
    from pytossinvest_mcp.paper import MemoryPaperStore
    paper = PaperBroker(MemoryPaperStore(starting_cash=settings.paper_starting_cash),
                        next_id=_counter("paper"))
```
(Or add `MemoryPaperStore` to the existing `from pytossinvest_mcp.paper import PaperBroker` import at top.)

- [ ] **Step 4: Run the full suite to verify green**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q`
Expected: PASS (full suite green on memory backend; `tools.py` unchanged because PaperBroker's public methods are stable).

- [ ] **Step 5: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/server.py pytossinvest-mcp/tests/conftest.py
git commit -m "refactor(mcp): wire memory paper store in server + conftest"
```

---

### Task 3: `redis_stores.py` — `RedisPaperStore`

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py`
- Test: `pytossinvest-mcp/tests/test_paper_redis.py` (new)

**Interfaces:**
- Consumes: `PaperState`, `Position`, `PaperOrder` from `paper.py` (runtime import — `paper.py` does NOT import `redis_stores.py`, so no cycle); `to_decimal`.
- Produces: `class RedisPaperStore` with `__init__(self, client, *, starting_cash, key="paper", lock_timeout=5.0)`, `lock()` (redis-py `Lock` on `lock:paper`), `load() -> PaperState` (returns a fresh starting state if the key is absent), `save(state)`. Money round-trips as decimal strings via `_paper_state_to_dict`/`_paper_state_from_dict`.

- [ ] **Step 1: Write the failing tests (fakeredis)**

```python
# pytossinvest-mcp/tests/test_paper_redis.py
from decimal import Decimal

import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.paper import PaperBroker, PaperError
from pytossinvest_mcp.redis_stores import RedisPaperStore


@pytest.fixture
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _broker(r, cash="1000000"):
    return PaperBroker(RedisPaperStore(r, starting_cash=cash))


def test_buy_then_holdings_decimal_exact(r):
    b = _broker(r, cash="1000")
    b.place(symbol="AAPL", side="BUY", order_type="LIMIT",
            fill_price="0.1", quantity="3", client_order_id="c1")
    h = b.holdings()
    assert h["cash"] == "999.7"                       # 1000 - 0.3, exact (not 999.6999...)
    assert h["items"][0]["averagePurchasePrice"] == "0.1"
    assert h["items"][0]["quantity"] == "3"


def test_sell_realizes_pnl(r):
    b = _broker(r, cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="2", client_order_id="c1")
    b.place(symbol="005930", side="SELL", order_type="LIMIT",
            fill_price="80000", quantity="1", client_order_id="c2")
    h = b.holdings()
    assert h["realizedPnl"] == "10000"               # (80000-70000)*1
    assert h["items"][0]["quantity"] == "1"


def test_insufficient_cash_raises(r):
    b = _broker(r, cash="100")
    with pytest.raises(PaperError, match="insufficient cash"):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="1", client_order_id="c1")


def test_place_idempotent_by_client_order_id(r):
    b = _broker(r, cash="1000000")
    o1 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="dup")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="dup")
    assert o2.order_id == o1.order_id
    assert len(b.list_orders()) == 1


def test_two_brokers_share_state(r):
    a = _broker(r, cash="1000000")
    b = PaperBroker(RedisPaperStore(r, starting_cash="1000000"))  # same fakeredis
    a.place(symbol="005930", side="BUY", order_type="LIMIT",
            fill_price="70000", quantity="1", client_order_id="c1")
    # instance b sees instance a's fill
    assert b.sellable_quantity("005930") == Decimal("1")
    assert b.holdings()["items"][0]["quantity"] == "1"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_paper_redis.py -v`
Expected: FAIL — `cannot import name 'RedisPaperStore'`.

- [ ] **Step 3: Implement `RedisPaperStore` in `redis_stores.py`**

Add to the existing imports: `from .paper import PaperState, Position, PaperOrder`. Append:

```python
def _paper_state_to_dict(state: PaperState) -> dict:
    return {
        "cash": str(state.cash),
        "realized_pnl": str(state.realized_pnl),
        "counter": state.counter,
        "positions": {
            s: {"quantity": str(p.quantity), "avg_price": str(p.avg_price)}
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
    return PaperState(
        cash=to_decimal(d["cash"]),
        realized_pnl=to_decimal(d["realized_pnl"]),
        counter=d["counter"],
        positions={
            s: Position(quantity=to_decimal(p["quantity"]), avg_price=to_decimal(p["avg_price"]))
            for s, p in d["positions"].items()
        },
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


class RedisPaperStore:
    def __init__(self, client, *, starting_cash, key: str = "paper", lock_timeout: float = 5.0):
        self._r = client
        self._key = key
        self._starting = to_decimal(starting_cash)
        self._lock_timeout = lock_timeout

    def lock(self):
        return self._r.lock(f"lock:{self._key}", timeout=self._lock_timeout,
                            blocking_timeout=self._lock_timeout)

    def load(self) -> PaperState:
        raw = self._r.get(self._key)
        if raw is None:
            return PaperState(cash=self._starting, positions={}, orders=[],
                              realized_pnl=Decimal("0"), counter=0)
        return _paper_state_from_dict(json.loads(raw))

    def save(self, state: PaperState) -> None:
        self._r.set(self._key, json.dumps(_paper_state_to_dict(state)))
```

(`json` and `Decimal` are already imported at the top of `redis_stores.py` from Plan 1.)

- [ ] **Step 4: Run to verify pass + full suite**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_paper_redis.py -v`
Expected: PASS (5 tests).
Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q`
Expected: PASS (full suite, 0 skips).

- [ ] **Step 5: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py pytossinvest-mcp/tests/test_paper_redis.py
git commit -m "feat(mcp): redis paper store (lock + decimal-string state + clientOrderId dedup)"
```

---

### Task 4: Backend selection for paper + parity/concurrency tests

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/server.py`
- Modify: `pytossinvest-mcp/tests/conftest.py`
- Test: `pytossinvest-mcp/tests/test_backend_parity.py` (extend)

**Interfaces:**
- Produces: `build_app_context` selects `RedisPaperStore` for `state_backend=="redis"` (sharing the same `redis.Redis` client used for token/spend/audit), else `MemoryPaperStore`. `conftest.make_app(backend=...)` wires the matching paper store.

- [ ] **Step 1: Write the failing tests (paper parity + concurrent dedup)**

Add to `test_backend_parity.py`:
```python
def test_paper_place_parity(app_factory, backend):
    app = app_factory(mode="paper", backend=backend)
    prev = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                           quantity="1", price="70000")
    res = T.place_order(app, confirmation_token=prev["confirmationToken"])
    assert res["status"] == "FILLED"
    h = T.get_holdings(app)
    assert h["items"][0]["quantity"] == "1"
```
Add to `test_paper_redis.py` (concurrent same-clientOrderId via two brokers on one fakeredis):
```python
def test_concurrent_same_coid_single_fill(r):
    a = _broker(r, cash="1000000")
    b = PaperBroker(RedisPaperStore(r, starting_cash="1000000"))
    o1 = a.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="same")
    o2 = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                 fill_price="70000", quantity="1", client_order_id="same")
    assert o1.order_id == o2.order_id           # dedup across instances
    assert len(a.list_orders()) == 1            # one fill total
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_backend_parity.py::test_paper_place_parity pytossinvest-mcp/tests/test_paper_redis.py::test_concurrent_same_coid_single_fill -v`
Expected: FAIL — redis-backed `app_factory` still builds a `MemoryPaperStore` (paper not yet backend-selected), so the redis-param parity test does not actually use redis; the concurrent test fails if dedup is not shared (it will pass only once paper is redis-backed AND dedup is inside the lock — confirm it genuinely exercises redis).

- [ ] **Step 3: Backend-select the paper store in `server.py`**

In `build_app_context`, replace the unconditional `MemoryPaperStore` (from Task 2) with a backend-aware choice that reuses the redis client. Simplest: extend `_build_stores` to also return the paper store, OR add a small `_build_paper_store(settings)` that, for redis, calls `_redis_from_url(settings.redis_url)`. To avoid opening two redis connections, prefer returning the paper store from the same place the other redis stores are built. Concretely, change `_build_stores` to also build and return the paper store:

```python
def _build_stores(settings: Settings):
    if settings.state_backend == "redis":
        from .redis_stores import RedisTokenStore, RedisSpendStore, RedisPaperStore
        from .audit import RedisAuditSink
        r = _redis_from_url(settings.redis_url)
        return (RedisTokenStore(r), RedisSpendStore(r),
                RedisAuditSink(r),
                RedisPaperStore(r, starting_cash=settings.paper_starting_cash))
    from .stores import MemoryTokenStore, MemorySpendStore
    from .paper import MemoryPaperStore
    return (MemoryTokenStore(), MemorySpendStore(),
            AuditLog(settings.audit_log_path),
            MemoryPaperStore(starting_cash=settings.paper_starting_cash))
```

And in `build_app_context`:
```python
    token_store, spend_store, audit, paper_store = _build_stores(settings)
    paper = PaperBroker(paper_store)
    safety = SafetyManager(settings, now=_time.monotonic,
                           today=lambda: datetime.now(_KST).date(),
                           token_store=token_store, spend_store=spend_store)
    safety.restore_spend(audit.read_events())
    return AppContext(config=settings, client=client, paper=paper, safety=safety,
                      audit=audit, now_kst=lambda: datetime.now(_KST))
```
(Remove the Task-2 inline `MemoryPaperStore` construction now that `_build_stores` owns it.)

- [ ] **Step 4: Backend-select the paper store in `conftest.make_app`**

Have `_make_stores(backend)` also return a paper store, and `make_app` build the broker from it:
```python
def _make_stores(backend):
    if backend == "redis":
        import fakeredis
        from pytossinvest_mcp.redis_stores import RedisTokenStore, RedisSpendStore, RedisPaperStore
        r = fakeredis.FakeStrictRedis(decode_responses=True)
        return (RedisTokenStore(r), RedisSpendStore(r),
                RedisPaperStore(r, starting_cash="10000000"))
    from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore
    from pytossinvest_mcp.paper import MemoryPaperStore
    return (MemoryTokenStore(), MemorySpendStore(),
            MemoryPaperStore(starting_cash="10000000"))
```
and in `make_app`:
```python
    token_store, spend_store, paper_store = _make_stores(backend)
    paper = PaperBroker(paper_store, next_id=_counter("paper"))
    safety = SafetyManager(settings, now=lambda: 1000.0, today=lambda: date(2026, 6, 17),
                           gen_id=_counter("cli"), token_store=token_store, spend_store=spend_store)
```
(`MemoryPaperStore`'s `starting_cash` should match `settings.paper_starting_cash`'s default; if a test overrides `paper_starting_cash`, thread it through — otherwise the default `"10000000"` is fine and matches the current default.)

- [ ] **Step 5: Run the targeted tests + full suite**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_backend_parity.py pytossinvest-mcp/tests/test_paper_redis.py -v`
Expected: PASS (paper parity on both backends; concurrent dedup).
Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q`
Expected: PASS (full suite, 0 skips).

- [ ] **Step 6: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/server.py pytossinvest-mcp/tests/conftest.py pytossinvest-mcp/tests/test_backend_parity.py pytossinvest-mcp/tests/test_paper_redis.py
git commit -m "feat(mcp): backend-select paper store; paper parity + concurrent-dedup tests"
```

---

### Task 5: Docs self-update

**Files:**
- Modify: `CLAUDE.md`, `docs/claude/pytossinvest-mcp.md`

- [ ] **Step 1: Update `CLAUDE.md`**
- Commands: update the MCP test count to the new total (run the suite first to get the exact number).
- Remove/adjust the Plan-1 interim limitation note that said "redis 백엔드 + 멀티인스턴스에서 paper 상태는 인스턴스별" — paper state is now externalized; under redis it is shared and survives restart.
- Add to the 함정 list (one line): paper state externalized via `PaperStore` (memory|redis); redis paper = single JSON key under a `lock:paper` redis-py Lock, money as decimal strings, `place()` idempotent by `clientOrderId` (repeated id returns the existing order, no second fill).
- Update the MCP 안전모델 / Conventions line if it described paper as in-memory-only.

- [ ] **Step 2: Update `docs/claude/pytossinvest-mcp.md`**
- In the state-backend section, add paper: `PaperStore` seam (`MemoryPaperStore` default | `RedisPaperStore`), `PaperState` (cash/positions/orders/realized_pnl/counter, money as decimal strings), `lock:paper` redis Lock, `clientOrderId` dedup inside the lock, redis key `paper`.
- Update any "paper 즉시체결 / in-memory" descriptions to note the store seam. Keep the "paper modify/cancel is live-only" pitfall (unchanged).

- [ ] **Step 3: Verify suites**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q` (green) and `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q` (59, untouched).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/claude/pytossinvest-mcp.md
git commit -m "docs(mcp): paper state externalization (PaperStore memory|redis)"
```

---

## Self-Review

**1. Spec coverage:** Spec §3 ("paper 도 동일 분산락 — place 를 account 단위 Redis 락으로 감싸 Python 평단가 수학 유지, clientOrderId dedup 도 락 안에서") → Tasks 1 (memory + math + dedup), 3 (redis lock + decimal-string state), 4 (backend selection + concurrent dedup). Spec §4 paper note → Task 3/4. The Plan-1 interim limitation removal → Task 5. ✅

**2. Placeholder scan:** Every code step has complete code (PaperState, MemoryPaperStore, refactored PaperBroker, RedisPaperStore, serialization helpers, wiring, tests). No "TBD"/"add error handling".

**3. Type consistency:** `PaperStore.lock()/load()->PaperState/save(state)` identical across `paper.py` (Task 1) and `redis_stores.py` (Task 3). `PaperBroker(store, *, next_id=None)` constructor used identically in server (Task 2/4) and conftest (Task 2/4). `_paper_state_to_dict`/`_paper_state_from_dict` mirror `PaperState`/`Position`/`PaperOrder` fields. `_build_stores` returns a 4-tuple after Task 4 (token, spend, audit, paper) — all call sites updated in the same task.

**Money safety check:** every monetary field in redis paper JSON is a decimal string (`str(...)` on write, `to_decimal(...)` on read); no float, no Redis numeric ops. `test_buy_then_holdings_decimal_exact` (0.1×3 → 999.7 exact) guards this.

**Circular-import note:** the paper-store seam lives in `paper.py` (protocol + memory impl + `PaperState`), so `redis_stores.py` imports paper types at runtime while `paper.py` never imports `redis_stores.py` — no cycle (same discipline as Plan 1's safety/stores split).
