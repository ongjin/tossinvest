# Safety-State Externalization Implementation Plan (Phase 1, Plan 1/3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the MCP safety state (confirmation tokens + per-currency daily-cap accumulator) behind a pluggable store seam with a `memory` (default) and a `redis` backend, refactoring the daily cap from "finalize-on-success" to distributed-safe "reserve-first", so multiple instances can share safety state for HA.

**Architecture:** `SafetyManager` keeps all policy (guardrail thresholds, currency logic, token lifecycle) but delegates *storage* to injected `TokenStore` + `SpendStore`. Memory impls reproduce today's dict behavior; Redis impls use redis-py's built-in `Lock` + Python `Decimal` read-modify-write (NO custom Lua, money stays decimal-safe). The daily cap becomes an atomic, idempotent (keyed by `clientOrderId`) reservation taken at place-time and released on failure.

**Tech Stack:** Python 3.12, `pydantic-settings`, `redis` (optional extra), `fakeredis` (dev, network-free Redis for tests), `pytest`.

## Global Constraints

- **Money/quantity are NEVER float** — strings/`Decimal` end-to-end. Redis counters stored as **decimal strings**; arithmetic in Python `Decimal` under a lock. Redis `INCR`/`INCRBYFLOAT` are forbidden for money (not decimal-safe). (project CRITICAL RULE)
- **SDK public API must not change** — this plan touches `pytossinvest-mcp` only; `pytossinvest` is untouched.
- **place_order/modify safety invariant** — execution path goes through `check_guardrails`; confirmation tokens issued only by preview after guardrails pass. This plan *refines* "finalize-on-success" into "reserve-on-attempt / release-on-failure / commit-on-success" (equivalent cap enforcement, distributed-safe). Update CLAUDE.md invariant wording in the final task.
- **Tests: zero network, no live keys** — Redis path tested via `fakeredis`. MCP tools tested via `FakeClient` + paper engine.
- **Test imports** — in tests use `from conftest import ...` (pytest puts `tests/` on `sys.path`), never `from tests.conftest`.
- **No AI-authorship markers** anywhere (commit messages, comments, docs). Public OSS repo.
- **Commits happen on explicit user request** (project CRITICAL RULE). The `git commit` steps below mark intended commit points; batch them and confirm with the user before actually committing, unless the user authorized per-task commits for this execution.
- **Commands run from repo root** `/Users/cyj/workspace/personal/toss`. MCP tests: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`.

---

### Task 1: Config — `state_backend` + `redis_url`

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/config.py`
- Test: `pytossinvest-mcp/tests/test_config.py`

**Interfaces:**
- Produces: `Settings.state_backend: Literal["memory","redis"]` (default `"memory"`), `Settings.redis_url: str` (default `""`). Model validator raises `ValueError` when `state_backend=="redis"` and `redis_url==""`.

- [ ] **Step 1: Write the failing test**

```python
# append to pytossinvest-mcp/tests/test_config.py
import pytest
from pytossinvest_mcp.config import Settings


def test_state_backend_defaults_to_memory():
    s = Settings(_env_file=None)
    assert s.state_backend == "memory"
    assert s.redis_url == ""


def test_redis_backend_requires_url():
    with pytest.raises(ValueError, match="redis_url"):
        Settings(_env_file=None, state_backend="redis")


def test_redis_backend_with_url_ok():
    s = Settings(_env_file=None, state_backend="redis", redis_url="redis://localhost:6379/0")
    assert s.state_backend == "redis"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_config.py -v`
Expected: FAIL — `state_backend` not a valid field / no validation error raised.

- [ ] **Step 3: Add fields + validator**

In `config.py`, add fields after the audit block (around the existing `audit_log_path`):

```python
    # state backend (HA). redis requires redis_url too.
    state_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = ""
```

Add a model validator next to `_live_requires_allow`:

```python
    @model_validator(mode="after")
    def _redis_requires_url(self):
        if self.state_backend == "redis" and not self.redis_url:
            raise ValueError(
                "state_backend='redis' requires TOSSINVEST_REDIS_URL"
            )
        return self
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_config.py -v`
Expected: PASS (all config tests).

- [ ] **Step 5: Commit (intended point)**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/config.py pytossinvest-mcp/tests/test_config.py
git commit -m "feat(mcp): add state_backend/redis_url settings"
```

---

### Task 2: `stores.py` — protocols + memory implementations

**Files:**
- Create: `pytossinvest-mcp/src/pytossinvest_mcp/stores.py`
- Test: `pytossinvest-mcp/tests/test_stores_memory.py`

**Interfaces:**
- Consumes: `OrderSpec` from `safety.py` (opaque — stored/returned, not constructed here; imported under `TYPE_CHECKING` to avoid a cycle).
- Produces:
  - `class TokenStore(Protocol)`: `put(token: str, spec, *, expires_at: float, issued_at: float) -> None`; `get(token: str) -> tuple[spec, float, float] | None`; `delete(token: str) -> None`.
  - `class SpendStore(Protocol)`: `reserve(day: str, currency: str, delta: Decimal, cap: Decimal, dedup_key: str) -> bool`; `release(day: str, currency: str, delta: Decimal, dedup_key: str) -> None`; `current(day: str, currency: str) -> Decimal`; `seed(day: str, currency: str, amount: Decimal) -> None`.
  - `class MemoryTokenStore` (implements `TokenStore`), `class MemorySpendStore` (implements `SpendStore`).
  - Semantics: `reserve` is idempotent by `dedup_key` (re-reserve returns `True` without double-counting); `release` only rolls back an existing reservation (idempotent), 0-floored; `seed` adds (restore), 0-floored.

- [ ] **Step 1: Write the failing test**

```python
# pytossinvest-mcp/tests/test_stores_memory.py
from decimal import Decimal

from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore


def test_token_put_get_delete():
    s = MemoryTokenStore()
    s.put("t1", "SPEC", expires_at=100.0, issued_at=50.0)
    assert s.get("t1") == ("SPEC", 100.0, 50.0)
    s.delete("t1")
    assert s.get("t1") is None
    assert s.get("missing") is None


def test_spend_reserve_under_cap():
    s = MemorySpendStore()
    assert s.reserve("2026-06-18", "KRW", Decimal("100"), Decimal("1000"), "c1") is True
    assert s.current("2026-06-18", "KRW") == Decimal("100")


def test_spend_reserve_over_cap_rejects_without_counting():
    s = MemorySpendStore()
    assert s.reserve("d", "KRW", Decimal("900"), Decimal("1000"), "c1") is True
    assert s.reserve("d", "KRW", Decimal("200"), Decimal("1000"), "c2") is False
    assert s.current("d", "KRW") == Decimal("900")  # rejected one not counted


def test_spend_reserve_is_idempotent_by_dedup():
    s = MemorySpendStore()
    assert s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1") is True
    assert s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1") is True  # same key
    assert s.current("d", "KRW") == Decimal("100")  # counted once


def test_spend_release_rolls_back_existing_only():
    s = MemorySpendStore()
    s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1")
    s.release("d", "KRW", Decimal("100"), "c1")
    assert s.current("d", "KRW") == Decimal("0")
    s.release("d", "KRW", Decimal("100"), "c1")  # idempotent, no underflow
    assert s.current("d", "KRW") == Decimal("0")
    # re-reserve after release works (fresh attempt)
    assert s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1") is True


def test_spend_negative_delta_downsize_and_release():
    s = MemorySpendStore()
    s.reserve("d", "KRW", Decimal("500"), Decimal("1000"), "base")
    # modify downsize: delta = -200
    assert s.reserve("d", "KRW", Decimal("-200"), Decimal("1000"), "m1") is True
    assert s.current("d", "KRW") == Decimal("300")
    s.release("d", "KRW", Decimal("-200"), "m1")  # rollback adds back
    assert s.current("d", "KRW") == Decimal("500")


def test_spend_seed_is_floored():
    s = MemorySpendStore()
    s.seed("d", "KRW", Decimal("100"))
    s.seed("d", "KRW", Decimal("50"))
    assert s.current("d", "KRW") == Decimal("150")
    s.seed("d", "KRW", Decimal("-1000"))  # floored at 0
    assert s.current("d", "KRW") == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_stores_memory.py -v`
Expected: FAIL — `ModuleNotFoundError: pytossinvest_mcp.stores`.

- [ ] **Step 3: Implement `stores.py`**

```python
# pytossinvest-mcp/src/pytossinvest_mcp/stores.py
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .safety import OrderSpec


class TokenStore(Protocol):
    def put(self, token: str, spec: "OrderSpec", *, expires_at: float, issued_at: float) -> None: ...
    def get(self, token: str) -> "tuple[OrderSpec, float, float] | None": ...
    def delete(self, token: str) -> None: ...


class SpendStore(Protocol):
    def reserve(self, day: str, currency: str, delta: Decimal, cap: Decimal, dedup_key: str) -> bool: ...
    def release(self, day: str, currency: str, delta: Decimal, dedup_key: str) -> None: ...
    def current(self, day: str, currency: str) -> Decimal: ...
    def seed(self, day: str, currency: str, amount: Decimal) -> None: ...


class MemoryTokenStore:
    def __init__(self) -> None:
        self._d: dict[str, tuple] = {}

    def put(self, token, spec, *, expires_at, issued_at):
        self._d[token] = (spec, expires_at, issued_at)

    def get(self, token):
        return self._d.get(token)

    def delete(self, token):
        self._d.pop(token, None)


class MemorySpendStore:
    def __init__(self) -> None:
        self._spent: dict[tuple[str, str], Decimal] = {}
        self._reserved: dict[str, set[str]] = {}

    def reserve(self, day, currency, delta, cap, dedup_key):
        seen = self._reserved.setdefault(day, set())
        if dedup_key in seen:
            return True
        cur = self._spent.get((day, currency), Decimal("0"))
        if cur + delta > cap:
            return False
        self._spent[(day, currency)] = cur + delta
        seen.add(dedup_key)
        return True

    def release(self, day, currency, delta, dedup_key):
        seen = self._reserved.get(day, set())
        if dedup_key not in seen:
            return
        seen.discard(dedup_key)
        cur = self._spent.get((day, currency), Decimal("0"))
        self._spent[(day, currency)] = max(Decimal("0"), cur - delta)

    def current(self, day, currency):
        return self._spent.get((day, currency), Decimal("0"))

    def seed(self, day, currency, amount):
        cur = self._spent.get((day, currency), Decimal("0"))
        self._spent[(day, currency)] = max(Decimal("0"), cur + amount)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_stores_memory.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit (intended point)**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/stores.py pytossinvest-mcp/tests/test_stores_memory.py
git commit -m "feat(mcp): add store seam (protocols + memory impls)"
```

---

### Task 3: `safety.py` — refactor to reserve-first over injected stores

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/safety.py`
- Modify (callers updated in Task 4): n/a here
- Test: `pytossinvest-mcp/tests/test_safety_tokens.py`, `pytossinvest-mcp/tests/test_safety_guardrails.py` (update existing), `pytossinvest-mcp/tests/test_safety_reserve.py` (new)

**Interfaces:**
- Consumes: `MemoryTokenStore`, `MemorySpendStore` from `stores.py`.
- Produces (new `SafetyManager` surface):
  - `__init__(config, *, now, today, gen_id=None, token_store, spend_store)` — `token_store`/`spend_store` are **required keyword args**.
  - `build_spec(...)` — unchanged.
  - `check_guardrails(spec, *, is_market_open, enforce_hours, check_daily=True, prev_notional=None)` — daily branch is now **read-only** (`spend_store.current(...)`, no mutation).
  - `reserve(spec) -> bool` — atomic idempotent daily reservation (delta = `notional` or `notional - prev_notional`).
  - `release(spec) -> None` — roll back a reservation.
  - `issue_token(spec) -> str`, `consume(token) -> OrderSpec`, `commit(token) -> None`.
  - `restore_spend(events) -> None` — seeds `spend_store` from `placed`/`modified` audit events.
  - **Removed:** `finalize`, `record_spend`, `_roll_daily`, in-memory `_pending`/`_spent`.

- [ ] **Step 1: Write the failing tests (new reserve-first behavior)**

```python
# pytossinvest-mcp/tests/test_safety_reserve.py
from datetime import date
from decimal import Decimal

import pytest

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.safety import SafetyManager, GuardrailError
from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore


def _mgr(now=1000.0, today=date(2026, 6, 18), **cfg):
    settings = Settings(_env_file=None, daily_order_limit=Decimal("1000"), **cfg)
    n = {"v": now}
    ids = {"i": 0}
    def gen():
        ids["i"] += 1
        return f"id-{ids['i']}"
    return SafetyManager(
        settings, now=lambda: n["v"], today=lambda: today, gen_id=gen,
        token_store=MemoryTokenStore(), spend_store=MemorySpendStore(),
    ), n


def _spec(mgr, *, qty="1", price="100", coid=None):
    s = mgr.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                       quantity=qty, price=price)
    if coid:
        s.client_order_id = coid
    return s


def test_reserve_then_commit_token_lifecycle():
    mgr, _ = _mgr()
    spec = _spec(mgr, qty="1", price="100")
    token = mgr.issue_token(spec)
    assert mgr.consume(token) is spec
    assert mgr.reserve(spec) is True
    mgr.commit(token)
    # token gone -> consume now raises
    with pytest.raises(GuardrailError, match="invalid-confirmation"):
        mgr.consume(token)


def test_reserve_rejects_over_daily_cap():
    mgr, _ = _mgr()
    big = _spec(mgr, qty="1", price="900", coid="c1")
    assert mgr.reserve(big) is True
    over = _spec(mgr, qty="1", price="200", coid="c2")
    assert mgr.reserve(over) is False  # 900+200 > 1000


def test_release_rolls_back_failed_attempt():
    mgr, _ = _mgr()
    spec = _spec(mgr, qty="1", price="600", coid="c1")
    assert mgr.reserve(spec) is True
    mgr.release(spec)
    # after release, a full-cap order fits again
    spec2 = _spec(mgr, qty="1", price="1000", coid="c2")
    assert mgr.reserve(spec2) is True


def test_check_guardrails_daily_is_read_only():
    mgr, _ = _mgr()
    spec = _spec(mgr, qty="1", price="100")
    mgr.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=True)
    mgr.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=True)
    # read-only: nothing was reserved by the checks
    assert mgr.reserve(_spec(mgr, qty="1", price="1000", coid="x")) is True


def test_expired_token_raises_and_is_deleted():
    mgr, clock = _mgr()
    spec = _spec(mgr)
    token = mgr.issue_token(spec)
    clock["v"] = 1000.0 + 9999
    with pytest.raises(GuardrailError, match="expired-confirmation"):
        mgr.consume(token)


def test_restore_spend_seeds_today_only():
    mgr, _ = _mgr()
    events = [
        {"decision": "placed", "ts": "2026-06-18T01:00:00+00:00",
         "currency": "KRW", "notional": "300"},
        {"decision": "placed", "ts": "2026-06-01T01:00:00+00:00",
         "currency": "KRW", "notional": "999"},  # old day, ignored
    ]
    mgr.restore_spend(events)
    # 300 seeded -> an 800 order fits, a 701 over the remaining cap rejects after
    assert mgr.reserve(_spec(mgr, qty="1", price="700", coid="a")) is True   # 300+700=1000 ok
    assert mgr.reserve(_spec(mgr, qty="1", price="1", coid="b")) is False     # 1000+1 > 1000
```

Note: `restore_spend` converts the UTC `ts` to the KST date — `2026-06-18T01:00:00+00:00` is `2026-06-18` in KST, matching `today`.

- [ ] **Step 2: Update the existing safety tests to the new API**

`test_safety_tokens.py` and `test_safety_guardrails.py` currently construct `SafetyManager(...)` without stores and call `finalize`/`record_spend`. Update every `SafetyManager(...)` construction to pass `token_store=MemoryTokenStore(), spend_store=MemorySpendStore()`, and replace assertions on `finalize`/`record_spend`/`_spent` with the new flow:
- `finalize(token, notional)` on success → `reserve(spec)` (before execute) + `commit(token)` (after success).
- `record_spend(n, cur)` → `reserve(spec)` for the equivalent spec, or `spend_store.seed(day, cur, n)` for direct seeding.
- daily-limit-at-place assertions → assert `reserve(spec)` returns `False` (instead of `check_guardrails` raising).

Run them red first:
Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_safety_tokens.py pytossinvest-mcp/tests/test_safety_guardrails.py -v`
Expected: FAIL (old API gone) until updated, then they exercise the new API.

- [ ] **Step 3: Run new + updated tests to verify they fail**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_safety_reserve.py -v`
Expected: FAIL — `SafetyManager.__init__() missing token_store/spend_store` / no `reserve`.

- [ ] **Step 4: Refactor `safety.py`**

Replace the `SafetyManager` body (keep `OrderSpec`, `GuardrailError`, `order_currency`, `_canon_symbol`, thresholds, `build_spec`, and the non-daily guardrail checks unchanged). Key changes:

```python
class SafetyManager:
    def __init__(self, config, *, now, today, gen_id=None, token_store, spend_store):
        self._cfg = config
        self._now = now
        self._today = today
        self._gen_id = gen_id or (lambda: uuid.uuid4().hex[:32])
        self.token_store = token_store
        self.spend_store = spend_store

    # build_spec: UNCHANGED

    def _daily_cap(self, currency):
        cfg = self._cfg
        return to_decimal(cfg.daily_order_limit_usd if currency == "USD" else cfg.daily_order_limit)

    def _delta(self, spec):
        if spec.prev_notional is None:
            return spec.notional
        return spec.notional - spec.prev_notional

    def check_guardrails(self, spec, *, is_market_open, enforce_hours,
                         check_daily=True, prev_notional=None):
        # ... per-order / high-value / hard-ceiling / deny-allow / market checks UNCHANGED ...
        if check_daily:
            day = self._today().isoformat()
            increment = spec.notional if prev_notional is None else spec.notional - prev_notional
            if self.spend_store.current(day, spec.currency) + increment > self._daily_cap(spec.currency):
                raise GuardrailError(
                    "daily-limit",
                    f"this order would push today's {spec.currency} total over the cap",
                )
        # ... enforce_hours check UNCHANGED ...

    def reserve(self, spec) -> bool:
        day = self._today().isoformat()
        return self.spend_store.reserve(
            day, spec.currency, self._delta(spec), self._daily_cap(spec.currency),
            spec.client_order_id,
        )

    def release(self, spec) -> None:
        day = self._today().isoformat()
        self.spend_store.release(day, spec.currency, self._delta(spec), spec.client_order_id)

    def issue_token(self, spec) -> str:
        token = self._gen_id()
        now = self._now()
        self.token_store.put(token, spec, expires_at=now + self._cfg.confirmation_ttl_sec,
                             issued_at=now)
        return token

    def consume(self, token):
        rec = self.token_store.get(token)
        if rec is None:
            raise GuardrailError("invalid-confirmation",
                                 "unknown or already-used confirmation_token; run preview again")
        spec, expires_at, issued_at = rec
        if self._now() > expires_at:
            self.token_store.delete(token)
            raise GuardrailError("expired-confirmation",
                                 "confirmation_token expired; run preview again")
        delay = self._cfg.live_confirm_min_delay_sec
        if self._cfg.is_live and delay > 0 and self._now() - issued_at < delay:
            raise GuardrailError("confirm-too-soon",
                                 f"live order must wait {delay}s after preview before placing")
        return spec

    def commit(self, token) -> None:
        self.token_store.delete(token)

    def restore_spend(self, events) -> None:
        today = self._today().isoformat()
        for ev in events:
            if not isinstance(ev, dict) or ev.get("decision") not in ("placed", "modified"):
                continue
            notional = ev.get("notional")
            ts = ev.get("ts")
            if notional is None or ts is None:
                continue
            try:
                ev_date = datetime.fromisoformat(ts).astimezone(_KST).date().isoformat()
                amount = to_decimal(notional)
            except (ValueError, TypeError, InvalidOperation):
                continue
            if ev_date != today:
                continue
            self.spend_store.seed(today, ev.get("currency", "KRW"), amount)
```

Keep the existing `check_guardrails` non-daily checks verbatim (copy from current file). Delete `finalize`, `record_spend`, `_roll_daily`, `_Pending`, and the `_pending`/`_spent`/`_spent_date` fields.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_safety_reserve.py pytossinvest-mcp/tests/test_safety_tokens.py pytossinvest-mcp/tests/test_safety_guardrails.py -v`
Expected: PASS.

- [ ] **Step 6: Commit (intended point)**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/safety.py pytossinvest-mcp/tests/test_safety_reserve.py pytossinvest-mcp/tests/test_safety_tokens.py pytossinvest-mcp/tests/test_safety_guardrails.py
git commit -m "refactor(mcp): safety daily cap to reserve-first over store seam"
```

---

### Task 4: `tools.py` — reserve-first place/modify flow + conftest

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/tools.py`
- Modify: `pytossinvest-mcp/tests/conftest.py`
- Test: `pytossinvest-mcp/tests/test_tools_write.py` (update)

**Interfaces:**
- Consumes: `SafetyManager.consume/reserve/release/commit/check_guardrails`, `GuardrailError`.
- Produces: `place_order`/`modify_order` that reserve before executing, release on failure, commit on success. `conftest.make_app(... backend="memory"|"redis" ...)` injects the right stores.

- [ ] **Step 1: Update `conftest.make_app` to inject stores (and a `backend` param)**

```python
# in conftest.py, replace the make_app body's SafetyManager construction
def make_app(fake_client, tmp_path, *, mode="paper", backend="memory", now_kst=None, **settings_kw):
    settings = Settings(_env_file=None, mode=mode,
                        audit_log_path=str(tmp_path / "audit.log"), **settings_kw)
    paper = PaperBroker(starting_cash=settings.paper_starting_cash, next_id=_counter("paper"))
    token_store, spend_store = _make_stores(backend)
    safety = SafetyManager(settings, now=lambda: 1000.0, today=lambda: date(2026, 6, 17),
                           gen_id=_counter("cli"),
                           token_store=token_store, spend_store=spend_store)
    audit = AuditLog(settings.audit_log_path)
    return AppContext(
        config=settings, client=fake_client, paper=paper, safety=safety, audit=audit,
        now_kst=now_kst or (lambda: datetime(2026, 6, 17, 10, 0, tzinfo=KST)),
    )


def _make_stores(backend):
    if backend == "redis":
        import fakeredis
        from pytossinvest_mcp.redis_stores import RedisTokenStore, RedisSpendStore
        r = fakeredis.FakeStrictRedis(decode_responses=True)
        return RedisTokenStore(r), RedisSpendStore(r)
    from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore
    return MemoryTokenStore(), MemorySpendStore()
```

Add imports at the top of conftest.py: `from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore` (the redis import stays lazy inside `_make_stores` so `fakeredis`/`redis` are only needed for the redis param).

- [ ] **Step 2: Write/adjust the failing test (daily-limit at place now via reserve)**

In `test_tools_write.py`, the test that asserts a second order over the daily cap is rejected at `place_order` should still pass (same observable `daily-limit` error). Add an explicit test that a failed execution releases the reservation:

```python
def test_place_failure_releases_reservation(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, daily_order_limit="1000000",
                      enforce_market_hours=False)

    def boom(**kwargs):
        raise RuntimeError("toss 500")
    fake_client.place_order = boom

    import pytossinvest_mcp.tools as T
    prev = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                           quantity="1", price="100000")
    with pytest.raises(RuntimeError):
        T.place_order(app, confirmation_token=prev["confirmationToken"])
    # reservation released: a fresh full-cap-adjacent order still previews/reserves fine
    spec = app.safety.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                                 quantity="1", price="100000")
    assert app.safety.reserve(spec) is True
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_tools_write.py::test_place_failure_releases_reservation -v`
Expected: FAIL — `place_order` still calls removed `finalize`.

- [ ] **Step 4: Rewrite `place_order` and `modify_order` flows**

Add import near the other safety imports at top of `tools.py`:

```python
from .safety import GuardrailError
```

`place_order` (replace the body around the existing consume/check/execute/finalize):

```python
def place_order(app, *, confirmation_token):
    spec = app.safety.consume(confirmation_token)
    # re-check non-daily guardrails (per-order/high-value/hard-ceiling/deny-allow); daily handled by reserve
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=False)
    if not app.safety.reserve(spec):
        raise GuardrailError("daily-limit", "this order would push today's total over the cap")
    try:
        ... existing paper/live execution block UNCHANGED ...
    except Exception as e:
        app.safety.release(spec)
        app.audit.record({
            "tool": "place_order", "mode": app.config.mode, "decision": "error",
            "error": str(e), "clientOrderId": spec.client_order_id,
        })
        raise  # token NOT committed -> idempotent retry reuses same clientOrderId

    app.safety.commit(confirmation_token)
    app.audit.record({
        "tool": "place_order", "mode": app.config.mode, "decision": "placed",
        "result": result, "clientOrderId": spec.client_order_id,
        "currency": spec.currency, "notional": spec.notional,
    })
    return result
```

`modify_order` (replace consume/check/execute/finalize):

```python
def modify_order(app, *, confirmation_token):
    spec = app.safety.consume(confirmation_token)
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=False)
    if not app.safety.reserve(spec):
        raise GuardrailError("daily-limit", "this modify would push today's total over the cap")
    try:
        result = app.client.modify_order(
            spec.modify_order_id, order_type=spec.order_type,
            price=spec.price, quantity=spec.quantity,
            confirm_high_value_order=spec.confirm_high_value_order,
        )
    except Exception as e:
        app.safety.release(spec)
        app.audit.record({
            "tool": "modify_order", "mode": app.config.mode, "decision": "error",
            "error": str(e), "orderId": spec.modify_order_id,
            "clientOrderId": spec.client_order_id,
        })
        raise

    app.safety.commit(confirmation_token)
    delta = spec.notional - (spec.prev_notional or Decimal("0"))
    app.audit.record({
        "tool": "modify_order", "mode": app.config.mode, "decision": "modified",
        "orderId": spec.modify_order_id, "result": result,
        "clientOrderId": spec.client_order_id, "notional": delta, "currency": spec.currency,
    })
    return result
```

(`reserve(spec)` internally uses the signed delta because `spec.prev_notional` is set on modify.)

- [ ] **Step 5: Run the full MCP suite (regression)**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -v`
Expected: PASS (all existing + new memory-backend tests green).

- [ ] **Step 6: Commit (intended point)**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/tools.py pytossinvest-mcp/tests/conftest.py pytossinvest-mcp/tests/test_tools_write.py
git commit -m "refactor(mcp): place/modify use reserve->commit/release flow"
```

---

### Task 5: `redis_stores.py` — Redis token + spend stores (Lock + Decimal)

**Files:**
- Create: `pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py`
- Test: `pytossinvest-mcp/tests/test_stores_redis.py`

**Interfaces:**
- Consumes: `OrderSpec` from `safety.py` (constructed during deserialization), a `redis.Redis` client (`decode_responses=True`).
- Produces: `RedisTokenStore(client, *, prefix="tok:", grace_sec=86400)`, `RedisSpendStore(client, *, lock_timeout=5.0)` implementing `TokenStore`/`SpendStore`. Counters stored as **decimal strings**; arithmetic in Python `Decimal` under `client.lock(...)`. Spec (de)serialized via `_spec_to_dict`/`_spec_from_dict` (Decimal fields ↔ strings).

- [ ] **Step 1: Write the failing test (run against fakeredis)**

```python
# pytossinvest-mcp/tests/test_stores_redis.py
from decimal import Decimal

import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.safety import SafetyManager
from pytossinvest_mcp.redis_stores import RedisTokenStore, RedisSpendStore
from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore


@pytest.fixture
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _spec():
    from datetime import date
    mgr = SafetyManager(Settings(_env_file=None), now=lambda: 0.0, today=lambda: date(2026, 6, 18),
                        token_store=MemoryTokenStore(), spend_store=MemorySpendStore())
    return mgr.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                          quantity="2", price="70000.5")  # decimal price


def test_token_roundtrip_preserves_decimal(r):
    s = RedisTokenStore(r)
    spec = _spec()
    s.put("t1", spec, expires_at=100.0, issued_at=50.0)
    got_spec, exp, iss = s.get("t1")
    assert (exp, iss) == (100.0, 50.0)
    assert got_spec.notional == spec.notional      # Decimal preserved exactly
    assert got_spec.price == "70000.5"
    assert got_spec.client_order_id == spec.client_order_id
    s.delete("t1")
    assert s.get("t1") is None


def test_spend_reserve_decimal_precise(r):
    s = RedisSpendStore(r)
    assert s.reserve("d", "USD", Decimal("0.1"), Decimal("1"), "c1") is True
    assert s.reserve("d", "USD", Decimal("0.2"), Decimal("1"), "c2") is True
    assert s.current("d", "USD") == Decimal("0.3")  # not 0.30000000000000004


def test_spend_reserve_idempotent_and_cap(r):
    s = RedisSpendStore(r)
    assert s.reserve("d", "KRW", Decimal("900"), Decimal("1000"), "c1") is True
    assert s.reserve("d", "KRW", Decimal("900"), Decimal("1000"), "c1") is True  # idempotent
    assert s.current("d", "KRW") == Decimal("900")
    assert s.reserve("d", "KRW", Decimal("200"), Decimal("1000"), "c2") is False


def test_spend_release_idempotent_floor(r):
    s = RedisSpendStore(r)
    s.reserve("d", "KRW", Decimal("100"), Decimal("1000"), "c1")
    s.release("d", "KRW", Decimal("100"), "c1")
    s.release("d", "KRW", Decimal("100"), "c1")  # idempotent
    assert s.current("d", "KRW") == Decimal("0")


def test_two_managers_share_token(r):
    from datetime import date
    cfg = Settings(_env_file=None)
    mk = lambda: SafetyManager(cfg, now=lambda: 0.0, today=lambda: date(2026, 6, 18),
                               gen_id=lambda: "fixed-token",
                               token_store=RedisTokenStore(r), spend_store=RedisSpendStore(r))
    a, b = mk(), mk()
    spec = a.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="1", price="100")
    token = a.issue_token(spec)
    # instance B can consume the token instance A issued
    got = b.consume(token)
    assert got.client_order_id == spec.client_order_id
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_stores_redis.py -v`
Expected: FAIL — `ModuleNotFoundError: pytossinvest_mcp.redis_stores` (or `fakeredis` not installed → install in Task 9; for now `importorskip` skips). To force the failure now, install fakeredis first: `uv sync --package pytossinvest-mcp --extra dev` after Task 9, or run after Task 9. If skipped, proceed and re-run at Task 8.

- [ ] **Step 3: Implement `redis_stores.py`**

```python
# pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py
from __future__ import annotations

import json
from decimal import Decimal

from pytossinvest.money import to_decimal

from .safety import OrderSpec


def _spec_to_dict(spec: OrderSpec) -> dict:
    return {
        "symbol": spec.symbol, "side": spec.side, "order_type": spec.order_type,
        "quantity": spec.quantity, "price": spec.price, "order_amount": spec.order_amount,
        "time_in_force": spec.time_in_force,
        "confirm_high_value_order": spec.confirm_high_value_order,
        "notional": str(spec.notional), "client_order_id": spec.client_order_id,
        "currency": spec.currency, "modify_order_id": spec.modify_order_id,
        "prev_notional": None if spec.prev_notional is None else str(spec.prev_notional),
    }


def _spec_from_dict(d: dict) -> OrderSpec:
    return OrderSpec(
        symbol=d["symbol"], side=d["side"], order_type=d["order_type"],
        quantity=d["quantity"], price=d["price"], order_amount=d["order_amount"],
        time_in_force=d["time_in_force"],
        confirm_high_value_order=d["confirm_high_value_order"],
        notional=to_decimal(d["notional"]), client_order_id=d["client_order_id"],
        currency=d["currency"], modify_order_id=d["modify_order_id"],
        prev_notional=None if d["prev_notional"] is None else to_decimal(d["prev_notional"]),
    )


class RedisTokenStore:
    def __init__(self, client, *, prefix: str = "tok:", grace_sec: int = 86400):
        self._r = client
        self._prefix = prefix
        self._grace = grace_sec  # physical TTL = (expires_at - now) is enforced in code; this is GC backstop

    def _key(self, token: str) -> str:
        return f"{self._prefix}{token}"

    def put(self, token, spec, *, expires_at, issued_at):
        payload = json.dumps({"spec": _spec_to_dict(spec),
                              "expires_at": expires_at, "issued_at": issued_at})
        # GC backstop TTL well beyond logical expiry; code checks expires_at for invalid/expired distinction
        self._r.set(self._key(token), payload, ex=self._grace)

    def get(self, token):
        raw = self._r.get(self._key(token))
        if raw is None:
            return None
        d = json.loads(raw)
        return _spec_from_dict(d["spec"]), d["expires_at"], d["issued_at"]

    def delete(self, token):
        self._r.delete(self._key(token))


class RedisSpendStore:
    def __init__(self, client, *, lock_timeout: float = 5.0, ttl_sec: int = 172800):
        self._r = client
        self._lock_timeout = lock_timeout
        self._ttl = ttl_sec  # 2 days; key naturally rolls by day, TTL is cleanup

    def _spend_key(self, day, currency):
        return f"spend:{day}:{currency}"

    def _reserved_key(self, day):
        return f"reserved:{day}"

    def _lock(self, day, currency):
        return self._r.lock(f"lock:spend:{day}:{currency}",
                            timeout=self._lock_timeout, blocking_timeout=self._lock_timeout)

    def reserve(self, day, currency, delta, cap, dedup_key):
        with self._lock(day, currency):
            if self._r.sismember(self._reserved_key(day), dedup_key):
                return True
            cur = to_decimal(self._r.get(self._spend_key(day, currency)) or "0")
            if cur + delta > cap:
                return False
            self._r.set(self._spend_key(day, currency), str(cur + delta), ex=self._ttl)
            self._r.sadd(self._reserved_key(day), dedup_key)
            self._r.expire(self._reserved_key(day), self._ttl)
            return True

    def release(self, day, currency, delta, dedup_key):
        with self._lock(day, currency):
            if not self._r.sismember(self._reserved_key(day), dedup_key):
                return
            self._r.srem(self._reserved_key(day), dedup_key)
            cur = to_decimal(self._r.get(self._spend_key(day, currency)) or "0")
            new = cur - delta
            if new < 0:
                new = Decimal("0")
            self._r.set(self._spend_key(day, currency), str(new), ex=self._ttl)

    def current(self, day, currency):
        return to_decimal(self._r.get(self._spend_key(day, currency)) or "0")

    def seed(self, day, currency, amount):
        # redis backend is the source of truth across restarts; restore is a no-op.
        return None
```

Note on `seed`: redis counters survive restarts (AOF), so `restore_spend` must NOT re-add audit deltas (would double count). `seed` is intentionally a no-op for redis.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_stores_redis.py -v`
Expected: PASS (after fakeredis installed in Task 9; if running before Task 9, do Task 9 first).

- [ ] **Step 5: Commit (intended point)**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py pytossinvest-mcp/tests/test_stores_redis.py
git commit -m "feat(mcp): redis token/spend stores (lock + decimal RMW)"
```

---

### Task 6: `audit.py` — Redis stream sink

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/audit.py`
- Test: `pytossinvest-mcp/tests/test_audit.py` (add)

**Interfaces:**
- Produces: `RedisAuditSink(client, *, stream="audit", maxlen=100000)` with `record(event: dict) -> None` and `read_events() -> list[dict]` — same surface as `AuditLog`. `record` stamps `ts` (UTC iso) like `AuditLog`. Values JSON-encoded per field (Decimals via `default=str`).

- [ ] **Step 1: Write the failing test**

```python
# add to pytossinvest-mcp/tests/test_audit.py
import pytest

fakeredis = pytest.importorskip("fakeredis")

from pytossinvest_mcp.audit import RedisAuditSink


def test_redis_audit_record_and_read():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sink = RedisAuditSink(r, now=lambda: __import__("datetime").datetime(2026, 6, 18,
                          tzinfo=__import__("datetime").timezone.utc))
    sink.record({"tool": "place_order", "decision": "placed", "notional": "100"})
    events = sink.read_events()
    assert len(events) == 1
    assert events[0]["decision"] == "placed"
    assert events[0]["notional"] == "100"
    assert events[0]["ts"].startswith("2026-06-18")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_audit.py -v`
Expected: FAIL — `cannot import name 'RedisAuditSink'`.

- [ ] **Step 3: Implement `RedisAuditSink` in `audit.py`**

```python
# append to audit.py
import json
from datetime import datetime, timezone


class RedisAuditSink:
    """Append-only audit via a Redis stream. Same surface as AuditLog."""

    def __init__(self, client, *, stream: str = "audit", maxlen: int = 100_000,
                 now=lambda: datetime.now(timezone.utc)):
        self._r = client
        self._stream = stream
        self._maxlen = maxlen
        self._now = now

    def record(self, event: dict) -> None:
        entry = {"ts": self._now().isoformat(), **event}
        flat = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))
                for k, v in entry.items()}
        self._r.xadd(self._stream, flat, maxlen=self._maxlen, approximate=True)

    def read_events(self) -> list[dict]:
        out = []
        for _id, fields in self._r.xrange(self._stream):
            ev = {}
            for k, v in fields.items():
                try:
                    ev[k] = json.loads(v)
                except (ValueError, TypeError):
                    ev[k] = v
            out.append(ev)
        return out
```

Note: string fields are stored raw; non-strings JSON-encoded. `read_events` tries `json.loads` per field, falling back to the raw string (so `"placed"` stays `"placed"`, `100` round-trips as int, `"100"` stays `"100"`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_audit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit (intended point)**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/audit.py pytossinvest-mcp/tests/test_audit.py
git commit -m "feat(mcp): redis stream audit sink"
```

---

### Task 7: `server.py` — backend selection + fail-closed

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/server.py`
- Test: `pytossinvest-mcp/tests/test_server_modes.py` (add)

**Interfaces:**
- Consumes: `Settings.state_backend/redis_url`, the memory + redis stores, `RedisAuditSink`.
- Produces: `build_app_context(settings, *, client)` that, for `state_backend=="redis"`, builds a `redis.Redis.from_url(settings.redis_url, decode_responses=True)` and wires `RedisTokenStore`/`RedisSpendStore`/`RedisAuditSink`; for `memory`, wires the in-memory stores + file `AuditLog` and runs `restore_spend`. A helper `_build_stores(settings)` returns `(token_store, spend_store, audit)`.

- [ ] **Step 1: Write the failing test (memory path unchanged + redis path wires redis stores)**

```python
# add to pytossinvest-mcp/tests/test_server_modes.py
import pytest

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.server import build_app_context


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_server_modes.py -v`
Expected: FAIL — `build_app_context` doesn't branch on backend / no `_redis_from_url`.

- [ ] **Step 3: Implement backend selection in `server.py`**

```python
# replace build_app_context, add helpers
def _redis_from_url(url: str):
    import redis  # optional dependency, imported only for redis backend
    return redis.Redis.from_url(url, decode_responses=True)


def _build_stores(settings: Settings):
    if settings.state_backend == "redis":
        from .redis_stores import RedisTokenStore, RedisSpendStore
        from .audit import RedisAuditSink
        r = _redis_from_url(settings.redis_url)
        return RedisTokenStore(r), RedisSpendStore(r), RedisAuditSink(r)
    from .stores import MemoryTokenStore, MemorySpendStore
    return MemoryTokenStore(), MemorySpendStore(), AuditLog(settings.audit_log_path)


def build_app_context(settings: Settings, *, client) -> AppContext:
    paper = PaperBroker(starting_cash=settings.paper_starting_cash)
    token_store, spend_store, audit = _build_stores(settings)
    safety = SafetyManager(
        settings, now=_time.monotonic, today=lambda: datetime.now(_KST).date(),
        token_store=token_store, spend_store=spend_store,
    )
    safety.restore_spend(audit.read_events())  # memory: rebuild today; redis: seed is no-op
    return AppContext(
        config=settings, client=client, paper=paper, safety=safety, audit=audit,
        now_kst=lambda: datetime.now(_KST),
    )
```

(`restore_spend` is safe to call for both backends: redis `seed` is a no-op, so reading the audit stream and "seeding" does nothing — the redis counter is already authoritative.)

- [ ] **Step 4: Write the fail-closed test**

```python
# add to test_server_modes.py
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
    # reserve must not silently pass when redis is unreachable
    with pytest.raises((ConnectionError, GuardrailError)):
        app.safety.reserve(spec)
```

This test documents the fail-closed contract: a Redis failure surfaces as an exception (the tool layer turns it into a refusal), never a silent pass. If you want a uniform `state-unavailable` GuardrailError instead of the raw `ConnectionError`, wrap store calls in `safety.reserve`/`consume` with `try/except (ConnectionError, TimeoutError) -> raise GuardrailError("state-unavailable", ...)`. Decide and make the test assert exactly one of the two; recommended: wrap and assert `GuardrailError`.

- [ ] **Step 5: (Recommended) wrap store errors as `state-unavailable`**

In `safety.py`, wrap the store-touching methods:

```python
import redis  # only if available; better: catch by duck-typed exception types
# Prefer not importing redis in safety.py. Instead catch broad connection errors:

def _guard_store(fn):
    try:
        return fn()
    except (ConnectionError, TimeoutError, OSError) as e:
        raise GuardrailError("state-unavailable",
                             f"order state store is unavailable: {e}") from e
```

Apply in `reserve`, `release`, `consume`, `issue_token`, `commit`:
```python
    def reserve(self, spec) -> bool:
        day = self._today().isoformat()
        return _guard_store(lambda: self.spend_store.reserve(
            day, spec.currency, self._delta(spec), self._daily_cap(spec.currency),
            spec.client_order_id))
```
(redis-py raises `redis.exceptions.ConnectionError`, which subclasses the builtin `ConnectionError`, so the builtin catch suffices — no `redis` import needed in `safety.py`.)

Update the fail-closed test to assert `GuardrailError(match="state-unavailable")`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_server_modes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit (intended point)**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/server.py pytossinvest-mcp/src/pytossinvest_mcp/safety.py pytossinvest-mcp/tests/test_server_modes.py
git commit -m "feat(mcp): backend selection in server + fail-closed on store errors"
```

---

### Task 8: Backend parity tests (memory vs fakeredis)

**Files:**
- Test: `pytossinvest-mcp/tests/test_backend_parity.py` (new)

**Interfaces:**
- Consumes: `conftest.app_factory(backend=...)`, `pytossinvest_mcp.tools`.
- Produces: parametrized tests proving the same preview→place scenario yields the same observable result on both backends.

- [ ] **Step 1: Write the parity test**

```python
# pytossinvest-mcp/tests/test_backend_parity.py
import pytest

import pytossinvest_mcp.tools as T


@pytest.fixture(params=["memory", "redis"])
def backend(request):
    if request.param == "redis":
        pytest.importorskip("fakeredis")
    return request.param


def test_preview_place_parity(app_factory, backend):
    app = app_factory(mode="paper", backend=backend)
    prev = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                           quantity="1", price="70000")
    res = T.place_order(app, confirmation_token=prev["confirmationToken"])
    assert res["status"] == "FILLED"
    assert res["clientOrderId"] == prev["clientOrderId"]


def test_daily_cap_parity(app_factory, backend):
    app = app_factory(mode="paper", backend=backend, daily_order_limit="100000")
    p1 = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="1", price="70000")
    T.place_order(app, confirmation_token=p1["confirmationToken"])
    p2 = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="1", price="70000")  # 140000 > 100000 cap
    from pytossinvest_mcp.safety import GuardrailError
    with pytest.raises(GuardrailError, match="daily-limit"):
        T.place_order(app, confirmation_token=p2["confirmationToken"])
```

- [ ] **Step 2: Run to verify both backends pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_backend_parity.py -v`
Expected: PASS for `[memory]` and `[redis]` params (redis via fakeredis).

- [ ] **Step 3: Commit (intended point)**

```bash
git add pytossinvest-mcp/tests/test_backend_parity.py
git commit -m "test(mcp): backend parity memory vs fakeredis"
```

---

### Task 9: Dependencies + docs self-update

**Files:**
- Modify: `pytossinvest-mcp/pyproject.toml`
- Modify: `CLAUDE.md`, `docs/claude/pytossinvest-mcp.md`

**Interfaces:**
- Produces: optional extra `redis = ["redis>=5"]`; `dev` extra gains `fakeredis>=2`; `pytest` marker `integration` registered.

- [ ] **Step 1: Update `pyproject.toml`**

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "fakeredis>=2"]
redis = ["redis>=5"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
markers = ["integration: requires a real Redis (opt-in, skipped by default)"]
```

- [ ] **Step 2: Sync dev deps**

Run: `uv sync --package pytossinvest-mcp --extra dev`
Expected: `fakeredis` installed.

- [ ] **Step 3: Run the FULL MCP suite (final regression)**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -v`
Expected: PASS — all existing tests + new memory + new redis(fakeredis) tests green.

- [ ] **Step 4: Run the SDK suite (confirm untouched)**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests`
Expected: PASS (59).

- [ ] **Step 5: Update docs (self-update obligation)**

In `CLAUDE.md`:
- Conventions / MCP 안전모델: note the **reserve-first** refinement of the place/modify invariant ("시도 시 예약 / 실패 시 해제 / 성공 시 유지"), and the new `state_backend`(memory|redis) axis + env vars (`TOSSINVEST_STATE_BACKEND`, `TOSSINVEST_REDIS_URL`).
- CRITICAL RULES `place_order` invariant: update finalize wording to reserve/commit/release.
- 함정: add "redis 백엔드는 카운터가 진실의 원천 → restore_spend(seed) no-op; memory 만 감사 리플레이 복원" and "돈은 Redis 에서도 decimal 문자열 + 분산락 RMW (INCRBYFLOAT 금지)".

In `docs/claude/pytossinvest-mcp.md`:
- Add a "상태 백엔드 (memory|redis)" section: store seam, reserve-first lifecycle, redis Lock+Decimal, fail-closed, audit stream. Update the place/modify token-lifecycle description.

- [ ] **Step 6: Commit (intended point)**

```bash
git add pytossinvest-mcp/pyproject.toml CLAUDE.md docs/claude/pytossinvest-mcp.md
git commit -m "chore(mcp): redis/fakeredis deps + docs for state backend"
```

---

## Self-Review

**1. Spec coverage:**
- §3 config (state_backend/redis_url) → Task 1 ✅
- §3 store seam + memory → Task 2 ✅
- §3/§4 reserve-first safety refactor → Task 3 ✅
- §4 place/modify flow → Task 4 ✅
- §3/§4 redis stores (Lock + Decimal, decimal-safe) → Task 5 ✅
- §3 audit redis stream → Task 6 ✅
- §3 backend selection + §5 fail-closed → Task 7 ✅
- §6 parity + decimal precision + token-HA + daily-cap → Tasks 5, 8 ✅
- §6 deps/markers + docs self-update → Task 9 ✅
- **Out of scope (separate plans):** paper-state externalization (§3/§4 paper lock) → Plan 2; transport + auth + docker (§1.3, §7 transport/auth vars, §8) → Plan 3. Config transport/auth fields are added in Plan 3 (added with their consumers, YAGNI).

**2. Placeholder scan:** No "TBD"/"add error handling"-style placeholders; every code step has complete code. The one judgment call (raw `ConnectionError` vs wrapped `state-unavailable`) is resolved in Task 7 Step 5 with a concrete recommendation and code.

**3. Type consistency:** `reserve(spec)->bool`, `release(spec)->None`, `commit(token)->None`, `consume(token)->OrderSpec`, `issue_token(spec)->str` used identically across Tasks 3/4/7. `SpendStore.reserve(day,currency,delta,cap,dedup_key)` and `release(day,currency,delta,dedup_key)` match between `stores.py` (Task 2) and `redis_stores.py` (Task 5). `_spec_to_dict`/`_spec_from_dict` mirror `OrderSpec` fields from `safety.py`.

**Interim limitation (documented):** with `state_backend=redis` and multiple instances, paper state is still per-instance until Plan 2 — safe (paper is non-safety-critical demo state); note in docs.
