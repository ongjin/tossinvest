# tossinvest-mcp Server Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `tossinvest-mcp`, an MCP server that gives an LLM safe read/trade access to a Toss Securities account by layering a 3-mode safety model (read_only / paper / live), guardrails, two-step preview→confirm ordering, idempotency, and an audit log on top of the finished `pytossinvest` SDK — fully testable without live credentials.

**Architecture:** A second member of the existing `uv` workspace, depending on `pytossinvest`. Layered: pure logic modules (`config`, `audit`, `paper`, `market_hours`, `safety`) → `tools.py` (one `fn(app, ...)` per MCP tool, routing paper-vs-real) → `server.py` (wraps tool functions in FastMCP closures and registers them by mode). Heavy logic is unit-tested by calling the modules/`tools.py` functions directly; the server layer is tested only for *which tools are registered per mode* via `await mcp.list_tools()`, so tests never depend on MCP transport internals.

**Tech Stack:** Python 3.12, `uv` (workspace), `mcp>=1.12` (FastMCP), `pydantic-settings>=2`, `pydantic` v2, `pytest` (async MCP calls driven via `asyncio.run`, no async plugin needed). License: Apache-2.0. Depends on `pytossinvest` (MIT).

**Design decisions locked from the spec (`docs/superpowers/specs/2026-06-17-tossinvest-mcp-design.md`) and the verified SDK surface:**
- **Modes** (`§3.1`): `read_only` (write tools never registered) / `paper` (default; orders fill a local `PaperBroker`) / `live` (real orders; requires `mode=live` **and** `allow_live=1` — a `model_validator` raises otherwise, so setting `mode=live` alone fails closed).
- **Paper = a coherent simulated account world.** Account-context tools (accounts, holdings, order_readiness, list/get_order, the order writes) route to `PaperBroker` in paper mode; **market-data tools** (quote, candles, stock_info, market_info) always hit the real client (market data is not account-scoped). Paper orders **fill instantly**; `modify_order`/`cancel_order` are live-only and raise a clear "already filled in paper" error (mirrors the real `409 already-filled`). This honestly bounds scope while satisfying the required buy→holdings→sell→realized-PnL demo.
- **Guardrails** (`§3.2`): per-order cap, daily cumulative cap, symbol allow/deny, high-value gate (`>= 100,000,000` requires explicit `confirm_high_value_order`; `> 3,000,000,000` always rejected). Caps/allow-deny/high-value enforced in **paper and live**; **market-hours** gate enforced **only in live** (paper demos run anytime).
- **Two-step ordering** (`§3.3`): `preview_order` validates + estimates + issues a short-lived `confirmation_token`; `place_order` refuses without a valid token. Token is **consumed on success only** — a failed `place_order` leaves it pending so a retry reuses the same auto-generated `clientOrderId` (idempotency, `§3.4`).
- **Money stays string/Decimal** end-to-end (`§5`); tool outputs serialize amounts as **strings** (JSON/Decimal-safe). The SDK already forbids float.
- **SDK surface is fixed** (Plan 1, merged): `TossInvestClient(client_id, client_secret, *, base_url=, timeout=, sleep=, monotonic=, now_kst=)`; reads return pydantic models (`Account`, `Price`, `BuyingPower`) or raw dict/list; `place_order`/`modify_order`/`cancel_order` as in Plan 1 Task 8. The MCP layer **only imports** the SDK; it never edits it.

---

## File Structure

```
toss/
  pyproject.toml                              # workspace root: add "tossinvest-mcp" to members
  tossinvest-mcp/
    pyproject.toml                            # package metadata, deps, Apache-2.0; console_script
    LICENSE                                   # Apache 2.0 full text
    NOTICE                                    # attribution
    README.md
    src/tossinvest_mcp/
      __init__.py                             # __version__
      config.py                               # Settings (pydantic-settings, env TOSSINVEST_*)
      audit.py                                # AuditLog (append JSONL, injected clock)
      paper.py                                # PaperBroker (sim cash/positions/fills/orders)
      market_hours.py                         # is_market_open(calendar, now_kst, country)
      safety.py                               # SafetyManager (guardrails + preview/confirm + idempotency)
      tools.py                                # AppContext + one fn(app, ...) per MCP tool
      server.py                               # build_server(settings, client) + main()
    tests/
      conftest.py                             # FakeClient + settings/app factories
      test_config.py
      test_audit.py
      test_paper.py
      test_market_hours.py
      test_safety.py
      test_tools_read.py
      test_tools_write.py
      test_server_modes.py
      test_smoke.py                           # package import + FastMCP harness smoke
```

Responsibilities — each file has one job, held in context independently: `config` (settings/validation, pure), `audit` (append log, file I/O), `paper` (sim engine, pure Decimal), `market_hours` (calendar→bool, pure), `safety` (guardrails + token lifecycle, pure + injected clocks), `tools` (route each tool paper-vs-real, no MCP imports), `server` (FastMCP registration + entrypoint).

---

## Task 1: Scaffold `tossinvest-mcp` + FastMCP harness smoke

**Files:**
- Modify: `pyproject.toml` (workspace root)
- Create: `tossinvest-mcp/pyproject.toml`
- Create: `tossinvest-mcp/LICENSE`, `tossinvest-mcp/NOTICE`, `tossinvest-mcp/README.md`
- Create: `tossinvest-mcp/src/tossinvest_mcp/__init__.py`
- Test: `tossinvest-mcp/tests/test_smoke.py`

- [ ] **Step 1: Add the package to the workspace root `pyproject.toml`**

Replace the file contents with:

```toml
[tool.uv.workspace]
members = ["pytossinvest", "tossinvest-mcp"]
```

- [ ] **Step 2: Create `tossinvest-mcp/pyproject.toml`**

```toml
[project]
name = "tossinvest-mcp"
version = "0.0.1"
description = "MCP server giving an LLM safe read/trade access to a Toss Securities account (unofficial)"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "Apache-2.0" }
dependencies = [
    "pytossinvest",
    "mcp>=1.12",
    "pydantic-settings>=2",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
tossinvest-mcp = "tossinvest_mcp.server:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tossinvest_mcp"]

[tool.uv.sources]
pytossinvest = { workspace = true }

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 3: Create `tossinvest-mcp/LICENSE`** — paste the **full standard Apache License 2.0 text** (the canonical document from https://www.apache.org/licenses/LICENSE-2.0.txt, unmodified).

- [ ] **Step 4: Create `tossinvest-mcp/NOTICE`**

```
tossinvest-mcp
Copyright 2026 ongjin

This product includes software developed as an unofficial client for the
Toss Securities Open API. It is not affiliated with, endorsed by, or
sponsored by Toss Securities. "Toss" is a trademark of its respective owner.
```

- [ ] **Step 5: Create `tossinvest-mcp/README.md`**

````markdown
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
````

- [ ] **Step 6: Create `tossinvest-mcp/src/tossinvest_mcp/__init__.py`**

```python
__version__ = "0.0.1"
```

- [ ] **Step 7: Write the smoke test `tossinvest-mcp/tests/test_smoke.py`**

This both checks the package imports **and** proves the FastMCP test harness (build a server, list its tools) works before we build real tools.

```python
import asyncio

import tossinvest_mcp


def test_version_exposed():
    assert tossinvest_mcp.__version__ == "0.0.1"


def test_fastmcp_harness_works():
    """Prove we can register a tool and read it back via list_tools() in-process."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("harness-check")

    @mcp.tool(name="ping", description="returns pong")
    def ping() -> dict:
        return {"reply": "pong"}

    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == {"ping"}
```

- [ ] **Step 8: Sync and run**

Run: `cd /Users/cyj/workspace/personal/toss && uv sync --package tossinvest-mcp --extra dev`
Then: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_smoke.py -v`
Expected: 2 passed. (If `list_tools()` is not awaitable in the installed `mcp` version, that is a blocking environment fact — stop and report; do not work around it.)

- [ ] **Step 9: Commit**

```bash
cd /Users/cyj/workspace/personal/toss
git add pyproject.toml tossinvest-mcp/
git commit -m "chore: scaffold tossinvest-mcp package (uv workspace, Apache-2.0, FastMCP smoke)"
```

---

## Task 2: `config.py` — settings, money validation, live double-gate

**Files:**
- Create: `tossinvest-mcp/src/tossinvest_mcp/config.py`
- Test: `tossinvest-mcp/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal

import pytest

from tossinvest_mcp.config import Settings


def _settings(**kw):
    # _env_file=None so a stray local .env never leaks into tests
    return Settings(_env_file=None, **kw)


def test_defaults_are_safe():
    s = _settings()
    assert s.mode == "paper"
    assert s.allow_live is False
    assert s.use_paper is True
    assert s.is_live is False


def test_money_fields_are_decimal_from_str():
    s = _settings(max_order_amount="2000000", daily_order_limit="9000000")
    assert s.max_order_amount == Decimal("2000000")
    assert isinstance(s.max_order_amount, Decimal)


def test_money_fields_reject_float():
    with pytest.raises(Exception):
        _settings(max_order_amount=1000000.5)


def test_live_without_allow_live_is_rejected():
    with pytest.raises(ValueError):
        _settings(mode="live", allow_live=False)


def test_live_with_allow_live_ok():
    s = _settings(mode="live", allow_live=True)
    assert s.is_live is True
    assert s.use_paper is False


def test_read_only_mode():
    s = _settings(mode="read_only")
    assert s.use_paper is False
    assert s.is_live is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tossinvest_mcp.config'`.

- [ ] **Step 3: Implement `config.py`**

```python
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOSSINVEST_", env_file=".env", extra="ignore"
    )

    # credentials / endpoint
    client_id: str = ""
    client_secret: str = ""
    base_url: str = "https://openapi.tossinvest.com"

    # mode (default safe). live requires allow_live too.
    mode: Literal["read_only", "paper", "live"] = "paper"
    allow_live: bool = False

    # guardrails (amounts are KRW-equivalent, conservative defaults)
    max_order_amount: Decimal = Decimal("1000000")
    daily_order_limit: Decimal = Decimal("5000000")
    allow_symbols: list[str] = []  # empty = allow all
    deny_symbols: list[str] = []
    enforce_market_hours: bool = True

    # paper engine
    paper_starting_cash: Decimal = Decimal("10000000")

    # preview -> confirm window
    confirmation_ttl_sec: int = 120

    # audit
    audit_log_path: str = "tossinvest-mcp-audit.log"

    @field_validator(
        "max_order_amount", "daily_order_limit", "paper_starting_cash", mode="before"
    )
    @classmethod
    def _no_float(cls, v):
        if isinstance(v, float):
            raise TypeError("money config must be a string or int, never float")
        return v

    @model_validator(mode="after")
    def _live_requires_allow(self):
        if self.mode == "live" and not self.allow_live:
            raise ValueError(
                "mode='live' requires TOSSINVEST_ALLOW_LIVE=1 (double safety gate)"
            )
        return self

    @property
    def use_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_config.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/config.py tossinvest-mcp/tests/test_config.py
git commit -m "feat(mcp): settings with money validation + live double-gate"
```

---

## Task 3: `audit.py` — append-only JSONL audit log

**Files:**
- Create: `tossinvest-mcp/src/tossinvest_mcp/audit.py`
- Test: `tossinvest-mcp/tests/test_audit.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from datetime import datetime, timezone
from decimal import Decimal

from tossinvest_mcp.audit import AuditLog


def _fixed_clock():
    return datetime(2026, 6, 17, 1, 2, 3, tzinfo=timezone.utc)


def test_record_appends_jsonl_with_timestamp(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, now=_fixed_clock)
    log.record({"tool": "place_order", "decision": "placed"})
    log.record({"tool": "preview_order", "decision": "previewed"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["tool"] == "place_order"
    assert first["decision"] == "placed"
    assert first["ts"] == "2026-06-17T01:02:03+00:00"


def test_record_serializes_decimal(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, now=_fixed_clock)
    log.record({"tool": "place_order", "notional": Decimal("70000")})
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["notional"] == "70000"


def test_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "audit.log"
    AuditLog(path, now=_fixed_clock).record({"tool": "x"})
    assert path.exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_audit.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `audit.py`**

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class AuditLog:
    """Append-only JSONL record of every write-tool decision. Trust/debug/blog evidence."""

    def __init__(
        self,
        path: "str | Path",
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._path = Path(path)
        self._now = now

    def record(self, event: dict) -> None:
        entry = {"ts": self._now().isoformat(), **event}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_audit.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/audit.py tossinvest-mcp/tests/test_audit.py
git commit -m "feat(mcp): append-only JSONL audit log (decimal-safe)"
```

---

## Task 4: `paper.py` — simulated broker (fills, holdings, buying power)

**Files:**
- Create: `tossinvest-mcp/src/tossinvest_mcp/paper.py`
- Test: `tossinvest-mcp/tests/test_paper.py`

The `PaperBroker` holds Decimal cash + positions and fills orders **instantly** (LIMIT at its price, MARKET at a caller-provided `fill_price`). Quantities/prices are strings/Decimal. Order ids come from an injected counter for deterministic tests.

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal

import pytest

from tossinvest_mcp.paper import PaperBroker, PaperError


def test_starts_with_configured_cash():
    b = PaperBroker(starting_cash="1000000")
    assert b.buying_power() == Decimal("1000000")
    assert b.holdings()["items"] == []


def test_buy_fills_and_reduces_cash():
    b = PaperBroker(starting_cash="1000000")
    order = b.place(symbol="005930", side="BUY", order_type="LIMIT",
                    fill_price="70000", quantity="10")
    assert order.status == "FILLED"
    assert order.order_id == "paper-1"
    assert b.cash == Decimal("300000")  # 1,000,000 - 70,000*10
    assert b.sellable_quantity("005930") == Decimal("10")


def test_buy_insufficient_cash_rejected():
    b = PaperBroker(starting_cash="100000")
    with pytest.raises(PaperError):
        b.place(symbol="005930", side="BUY", order_type="LIMIT",
                fill_price="70000", quantity="10")


def test_buy_then_sell_realizes_pnl():
    b = PaperBroker(starting_cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="65000", quantity="10")
    b.place(symbol="005930", side="SELL", order_type="LIMIT", fill_price="70000", quantity="10")
    assert b.realized_pnl == Decimal("50000")  # (70000-65000)*10
    assert b.sellable_quantity("005930") == Decimal("0")
    assert "005930" not in b.positions


def test_sell_more_than_held_rejected():
    b = PaperBroker(starting_cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="65000", quantity="5")
    with pytest.raises(PaperError):
        b.place(symbol="005930", side="SELL", order_type="LIMIT", fill_price="70000", quantity="10")


def test_average_price_updates_on_add():
    b = PaperBroker(starting_cash="10000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="60000", quantity="10")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="80000", quantity="10")
    pos = b.positions["005930"]
    assert pos.quantity == Decimal("20")
    assert pos.avg_price == Decimal("70000")


def test_holdings_and_orders_are_strings():
    b = PaperBroker(starting_cash="1000000")
    b.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="70000", quantity="10",
            client_order_id="cli-1")
    h = b.holdings()
    assert h["cash"] == "300000"
    assert h["items"][0] == {"symbol": "005930", "quantity": "10", "averagePurchasePrice": "70000"}
    listed = b.list_orders()
    assert listed[0].client_order_id == "cli-1"
    assert b.get_order("paper-1").symbol == "005930"
    assert b.get_order("nope") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_paper.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `paper.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from pytossinvest.money import to_decimal


class PaperError(Exception):
    """Paper-broker rule violation (insufficient cash/quantity, bad side)."""


@dataclass
class Position:
    quantity: Decimal
    avg_price: Decimal


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


class PaperBroker:
    def __init__(
        self,
        *,
        starting_cash: "str | int | Decimal" = "10000000",
        next_id: "Callable[[], str] | None" = None,
    ):
        self.cash: Decimal = to_decimal(starting_cash)
        self.positions: dict[str, Position] = {}
        self.orders: list[PaperOrder] = []
        self.realized_pnl: Decimal = Decimal("0")
        self._counter = 0
        self._next_id = next_id or self._default_id

    def _default_id(self) -> str:
        self._counter += 1
        return f"paper-{self._counter}"

    def buying_power(self) -> Decimal:
        return self.cash

    def sellable_quantity(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else Decimal("0")

    def place(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        fill_price: "str | int | Decimal",
        quantity: "str | int | Decimal",
        client_order_id: "str | None" = None,
    ) -> PaperOrder:
        price = to_decimal(fill_price)
        qty = to_decimal(quantity)
        if side == "BUY":
            cost = price * qty
            if cost > self.cash:
                raise PaperError(f"insufficient cash: need {cost}, have {self.cash}")
            self.cash -= cost
            pos = self.positions.get(symbol)
            if pos:
                total = pos.quantity + qty
                pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total
                pos.quantity = total
            else:
                self.positions[symbol] = Position(quantity=qty, avg_price=price)
        elif side == "SELL":
            pos = self.positions.get(symbol)
            if pos is None or pos.quantity < qty:
                have = pos.quantity if pos else Decimal("0")
                raise PaperError(f"insufficient quantity: need {qty}, have {have}")
            self.realized_pnl += (price - pos.avg_price) * qty
            self.cash += price * qty
            pos.quantity -= qty
            if pos.quantity == 0:
                del self.positions[symbol]
        else:
            raise PaperError(f"unknown side: {side}")

        order = PaperOrder(
            order_id=self._next_id(), symbol=symbol, side=side, order_type=order_type,
            quantity=qty, price=price, status="FILLED", client_order_id=client_order_id,
        )
        self.orders.append(order)
        return order

    def get_order(self, order_id: str) -> "PaperOrder | None":
        return next((o for o in self.orders if o.order_id == order_id), None)

    def list_orders(self) -> list[PaperOrder]:
        return list(self.orders)

    def holdings(self) -> dict:
        return {
            "cash": str(self.cash),
            "realizedPnl": str(self.realized_pnl),
            "items": [
                {
                    "symbol": s,
                    "quantity": str(p.quantity),
                    "averagePurchasePrice": str(p.avg_price),
                }
                for s, p in self.positions.items()
            ],
        }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_paper.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/paper.py tossinvest-mcp/tests/test_paper.py
git commit -m "feat(mcp): paper broker (instant fills, decimal cash/positions, realized pnl)"
```

---

## Task 5: `market_hours.py` — calendar → open/closed

**Files:**
- Create: `tossinvest-mcp/src/tossinvest_mcp/market_hours.py`
- Test: `tossinvest-mcp/tests/test_market_hours.py`

Reads today's `regularMarket` session from a `/market-calendar` response and decides if `now` (KST) is within it. Tolerant of missing/closed sessions (→ closed). KR uses `today.integrated.regularMarket`; US uses `today.regularMarket`.

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from tossinvest_mcp.market_hours import is_market_open

KST = ZoneInfo("Asia/Seoul")

KR_OPEN_DAY = {"today": {"integrated": {"regularMarket": {"startTime": "09:00", "endTime": "15:30"}}}}
KR_HOLIDAY = {"today": {"integrated": {}}}
US_OPEN_DAY = {"today": {"regularMarket": {"startTime": "23:30", "endTime": "06:00"}}}


def _kst(h, m):
    return datetime(2026, 6, 17, h, m, tzinfo=KST)


def test_kr_inside_session_is_open():
    assert is_market_open(KR_OPEN_DAY, _kst(10, 0), "KR") is True


def test_kr_before_open_is_closed():
    assert is_market_open(KR_OPEN_DAY, _kst(8, 59), "KR") is False


def test_kr_at_close_is_closed():
    # end is exclusive
    assert is_market_open(KR_OPEN_DAY, _kst(15, 30), "KR") is False


def test_kr_holiday_is_closed():
    assert is_market_open(KR_HOLIDAY, _kst(10, 0), "KR") is False


def test_us_session_read_from_regular_market():
    assert is_market_open(US_OPEN_DAY, _kst(23, 45), "US") is True


def test_unknown_shape_is_closed():
    assert is_market_open({}, _kst(10, 0), "KR") is False
    assert is_market_open(None, _kst(10, 0), "KR") is False


def test_malformed_time_is_closed():
    bad = {"today": {"integrated": {"regularMarket": {"startTime": "garbage", "endTime": "15:30"}}}}
    assert is_market_open(bad, _kst(10, 0), "KR") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_market_hours.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `market_hours.py`**

```python
from __future__ import annotations

from datetime import datetime, time


def _parse_hhmm(value: str) -> time:
    parts = value.split(":")
    return time(int(parts[0]), int(parts[1]))


def is_market_open(calendar: "dict | None", now_kst: datetime, country: str) -> bool:
    """Best-effort: open iff `now_kst` falls in today's regular-market session.

    Tolerates missing/closed/unknown shapes by returning False. The API gives all
    times in KST. A US session stated in KST wraps past midnight (e.g. 23:30->06:00);
    when startTime > endTime the window is treated as [start, 24:00) ∪ [00:00, end).
    The v1 hours gate runs only in live mode and can be overridden via
    enforce_market_hours=False.
    """
    today = (calendar or {}).get("today") or {}
    if country.upper() == "KR":
        session = (today.get("integrated") or {}).get("regularMarket") or {}
    else:
        session = today.get("regularMarket") or {}
    start, end = session.get("startTime"), session.get("endTime")
    if not start or not end:
        return False
    try:
        start_t, end_t = _parse_hhmm(start), _parse_hhmm(end)
    except (ValueError, IndexError):
        return False  # malformed time string -> treat as closed (safe default)
    now_t = now_kst.time()
    if start_t <= end_t:
        return start_t <= now_t < end_t
    # wraps past midnight (US session stated in KST)
    return now_t >= start_t or now_t < end_t
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_market_hours.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/market_hours.py tossinvest-mcp/tests/test_market_hours.py
git commit -m "feat(mcp): market-hours gate from calendar response (tolerant)"
```

---

## Task 6: `safety.py` — order spec, notional, guardrails

**Files:**
- Create: `tossinvest-mcp/src/tossinvest_mcp/safety.py`
- Test: `tossinvest-mcp/tests/test_safety_guardrails.py`

This task builds the `OrderSpec` dataclass, `SafetyManager.build_spec` (computes notional + assigns a `clientOrderId`), and `SafetyManager.check_guardrails`. Preview/confirm token lifecycle is Task 7 (same class, additional methods). The manager takes injected `now` (monotonic seconds) and `today` (date) for deterministic tests.

- [ ] **Step 1: Write the failing test**

```python
from datetime import date
from decimal import Decimal

import pytest

from tossinvest_mcp.config import Settings
from tossinvest_mcp.safety import SafetyManager, GuardrailError


def _ids():
    n = {"i": 0}
    def gen():
        n["i"] += 1
        return f"cli-{n['i']}"
    return gen


def _mgr(**overrides):
    s = Settings(_env_file=None, **overrides)
    return SafetyManager(s, now=lambda: 1000.0, today=lambda: date(2026, 6, 17), gen_id=_ids())


def test_build_spec_notional_quantity_based():
    m = _mgr()
    spec = m.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="10", price="70000")
    assert spec.notional == Decimal("700000")
    assert spec.client_order_id == "cli-1"


def test_build_spec_notional_amount_based():
    m = _mgr()
    spec = m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET", order_amount="100")
    assert spec.notional == Decimal("100")


def test_build_spec_market_quantity_uses_ref_price():
    m = _mgr()
    spec = m.build_spec(symbol="005930", side="BUY", order_type="MARKET",
                        quantity="3", ref_price="70000")
    assert spec.notional == Decimal("210000")


def test_build_spec_insufficient_params():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="005930", side="BUY", order_type="MARKET", quantity="3")
    assert e.value.code == "insufficient-order-params"


def _spec(m, **kw):
    base = dict(symbol="005930", side="BUY", order_type="LIMIT", quantity="1", price="70000")
    base.update(kw)
    return m.build_spec(**base)


def _ok(m, spec):
    m.check_guardrails(spec, is_market_open=True, enforce_hours=False)


def test_per_order_cap_rejects():
    m = _mgr(max_order_amount="1000000")
    spec = _spec(m, quantity="20", price="70000")  # 1,400,000 > cap
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "order-amount-cap"


def test_within_cap_passes():
    m = _mgr(max_order_amount="1000000")
    _ok(m, _spec(m, quantity="10", price="70000"))  # 700,000


def test_deny_list_rejects():
    m = _mgr(deny_symbols=["005930"])
    with pytest.raises(GuardrailError) as e:
        _ok(m, _spec(m))
    assert e.value.code == "symbol-denied"


def test_allow_list_rejects_others():
    m = _mgr(allow_symbols=["000660"])
    with pytest.raises(GuardrailError) as e:
        _ok(m, _spec(m, symbol="005930"))
    assert e.value.code == "symbol-not-allowed"


def test_high_value_requires_confirm():
    m = _mgr(max_order_amount="999999999999", daily_order_limit="999999999999")
    spec = _spec(m, quantity="2000", price="70000")  # 140,000,000 >= 1억
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "confirm-high-value-required"


def test_high_value_with_confirm_passes():
    m = _mgr(max_order_amount="999999999999", daily_order_limit="999999999999")
    spec = _spec(m, quantity="2000", price="70000", confirm_high_value_order=True)
    _ok(m, spec)


def test_above_max_threshold_always_rejected():
    m = _mgr(max_order_amount="999999999999999", daily_order_limit="999999999999999")
    spec = _spec(m, quantity="100000", price="70000", confirm_high_value_order=True)  # 7,000,000,000 > 30억
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "max-order-exceeded"


def test_daily_limit_accumulates():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    s1 = _spec(m, quantity="10", price="70000")  # 700,000
    m.check_guardrails(s1, is_market_open=True, enforce_hours=False)
    m.record_spend(s1.notional)
    s2 = _spec(m, quantity="10", price="70000")  # +700,000 -> 1,400,000 > 1,000,000
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(s2, is_market_open=True, enforce_hours=False)
    assert e.value.code == "daily-limit"


def test_market_closed_rejected_when_enforced():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(_spec(m), is_market_open=False, enforce_hours=True)
    assert e.value.code == "market-closed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `safety.py` (spec + guardrails; token methods added in Task 7)**

```python
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from pytossinvest.money import to_decimal

from .config import Settings

HIGH_VALUE_THRESHOLD = Decimal("100000000")    # 1억 KRW: requires explicit confirm
MAX_ORDER_THRESHOLD = Decimal("3000000000")    # 30억 KRW: always rejected


class GuardrailError(Exception):
    """An order rejected by a client-side safety guardrail (code-based)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class OrderSpec:
    symbol: str
    side: str
    order_type: str
    quantity: "str | None"
    price: "str | None"
    order_amount: "str | None"
    time_in_force: str
    confirm_high_value_order: bool
    notional: Decimal
    client_order_id: str


@dataclass
class _Pending:
    spec: OrderSpec
    expires_at: float


class SafetyManager:
    def __init__(
        self,
        config: Settings,
        *,
        now: Callable[[], float],
        today: Callable[[], date],
        gen_id: "Callable[[], str] | None" = None,
    ):
        self._cfg = config
        self._now = now          # monotonic seconds (token expiry)
        self._today = today      # date (daily-cap reset)
        self._gen_id = gen_id or (lambda: uuid.uuid4().hex[:32])
        self._pending: dict[str, _Pending] = {}
        self._spent_date: "date | None" = None
        self._spent: Decimal = Decimal("0")

    def build_spec(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: "str | None" = None,
        price: "str | None" = None,
        order_amount: "str | None" = None,
        time_in_force: str = "DAY",
        confirm_high_value_order: bool = False,
        ref_price: "str | None" = None,
    ) -> OrderSpec:
        if order_amount is not None:
            notional = to_decimal(order_amount)
        elif price is not None and quantity is not None:
            notional = to_decimal(price) * to_decimal(quantity)
        elif quantity is not None and ref_price is not None:
            notional = to_decimal(ref_price) * to_decimal(quantity)
        else:
            raise GuardrailError(
                "insufficient-order-params",
                "need price+quantity, order_amount, or quantity+ref_price",
            )
        return OrderSpec(
            symbol=symbol, side=side, order_type=order_type, quantity=quantity,
            price=price, order_amount=order_amount, time_in_force=time_in_force,
            confirm_high_value_order=confirm_high_value_order, notional=notional,
            client_order_id=self._gen_id(),
        )

    def check_guardrails(
        self, spec: OrderSpec, *, is_market_open: bool, enforce_hours: bool
    ) -> None:
        cfg = self._cfg
        if cfg.deny_symbols and spec.symbol in cfg.deny_symbols:
            raise GuardrailError("symbol-denied", f"{spec.symbol} is in the deny list")
        if cfg.allow_symbols and spec.symbol not in cfg.allow_symbols:
            raise GuardrailError("symbol-not-allowed", f"{spec.symbol} is not in the allow list")
        if spec.notional > MAX_ORDER_THRESHOLD:
            raise GuardrailError(
                "max-order-exceeded",
                f"notional {spec.notional} exceeds the hard 3,000,000,000 ceiling",
            )
        if spec.notional >= HIGH_VALUE_THRESHOLD and not spec.confirm_high_value_order:
            raise GuardrailError(
                "confirm-high-value-required",
                "orders >= 100,000,000 require confirm_high_value_order=true",
            )
        if spec.notional > to_decimal(cfg.max_order_amount):
            raise GuardrailError(
                "order-amount-cap",
                f"notional {spec.notional} exceeds per-order cap {cfg.max_order_amount}",
            )
        self._roll_daily()
        if self._spent + spec.notional > to_decimal(cfg.daily_order_limit):
            raise GuardrailError(
                "daily-limit",
                f"this order would push today's total over {cfg.daily_order_limit}",
            )
        if enforce_hours and not is_market_open:
            raise GuardrailError(
                "market-closed",
                "market is closed (set enforce_market_hours=false to override)",
            )

    def _roll_daily(self) -> None:
        d = self._today()
        if self._spent_date != d:
            self._spent_date = d
            self._spent = Decimal("0")

    def record_spend(self, notional: Decimal) -> None:
        self._roll_daily()
        self._spent += notional
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/tests/test_safety_guardrails.py
git commit -m "feat(mcp): order spec + guardrails (caps, allow/deny, high-value, daily, hours)"
```

---

## Task 7: `safety.py` — preview/confirm token + idempotency

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (add methods to `SafetyManager`)
- Test: `tossinvest-mcp/tests/test_safety_tokens.py`

`issue_token` stores the spec under a token with a TTL. `consume` validates the token exists and isn't expired and returns the spec **without removing it**. `finalize` removes it and records the spend — called only after a successful place. So a failed place leaves the token pending, and a retry reuses the same spec (same `clientOrderId` → idempotent); a successful place consumes it so it can't double-fire.

- [ ] **Step 1: Write the failing test**

```python
from datetime import date
from decimal import Decimal

import pytest

from tossinvest_mcp.config import Settings
from tossinvest_mcp.safety import SafetyManager, GuardrailError


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _mgr(clock, **overrides):
    s = Settings(_env_file=None, confirmation_ttl_sec=120, **overrides)
    return SafetyManager(s, now=clock, today=lambda: date(2026, 6, 17))


def _spec(m):
    return m.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="10", price="70000")


def test_issue_then_consume_returns_spec():
    clock = Clock()
    m = _mgr(clock)
    spec = _spec(m)
    token = m.issue_token(spec)
    got = m.consume(token)
    assert got.client_order_id == spec.client_order_id


def test_unknown_token_rejected():
    m = _mgr(Clock())
    with pytest.raises(GuardrailError) as e:
        m.consume("does-not-exist")
    assert e.value.code == "invalid-confirmation"


def test_expired_token_rejected():
    clock = Clock()
    m = _mgr(clock)
    token = m.issue_token(_spec(m))
    clock.advance(121)  # ttl is 120
    with pytest.raises(GuardrailError) as e:
        m.consume(token)
    assert e.value.code == "expired-confirmation"


def test_finalize_consumes_token_and_records_spend():
    clock = Clock()
    m = _mgr(clock, daily_order_limit="999999999")
    spec = _spec(m)
    token = m.issue_token(spec)
    m.consume(token)
    m.finalize(token, spec.notional)
    # second consume fails: token gone (no double-fire)
    with pytest.raises(GuardrailError) as e:
        m.consume(token)
    assert e.value.code == "invalid-confirmation"
    # spend was recorded toward the daily cap
    assert m._spent == Decimal("700000")


def test_failed_place_leaves_token_for_idempotent_retry():
    clock = Clock()
    m = _mgr(clock)
    spec = _spec(m)
    token = m.issue_token(spec)
    # simulate place attempt that consumes (validates) but does NOT finalize (failed)
    first = m.consume(token)
    # retry: same token still valid, same clientOrderId reused
    second = m.consume(token)
    assert first.client_order_id == second.client_order_id
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_tokens.py -v`
Expected: FAIL with `AttributeError: 'SafetyManager' object has no attribute 'issue_token'`.

- [ ] **Step 3: Add token methods to `SafetyManager`** (append after `record_spend`)

```python
    def issue_token(self, spec: OrderSpec) -> str:
        token = self._gen_id()
        self._pending[token] = _Pending(
            spec=spec, expires_at=self._now() + self._cfg.confirmation_ttl_sec
        )
        return token

    def consume(self, token: str) -> OrderSpec:
        pending = self._pending.get(token)
        if pending is None:
            raise GuardrailError(
                "invalid-confirmation",
                "unknown or already-used confirmation_token; run preview_order again",
            )
        if self._now() > pending.expires_at:
            del self._pending[token]
            raise GuardrailError(
                "expired-confirmation",
                "confirmation_token expired; run preview_order again",
            )
        return pending.spec

    def finalize(self, token: str, notional: Decimal) -> None:
        self._pending.pop(token, None)
        self.record_spend(notional)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_tokens.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/tests/test_safety_tokens.py
git commit -m "feat(mcp): preview/confirm token lifecycle (consume-on-success idempotency)"
```

---

## Task 8: `tools.py` — AppContext + read tools

**Files:**
- Create: `tossinvest-mcp/src/tossinvest_mcp/tools.py`
- Create: `tossinvest-mcp/tests/conftest.py`
- Test: `tossinvest-mcp/tests/test_tools_read.py`

`AppContext` bundles the wired dependencies. Read tools take `app` as the first argument and route account-context reads to the paper broker when `app.use_paper`, while market-data reads always use the real client. A `FakeClient` (in `conftest.py`) returns SDK-shaped objects so tools can be tested with no network.

- [ ] **Step 1: Write `conftest.py` (shared fakes/factories)**

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from pytossinvest.models import Account, BuyingPower, Price
from tossinvest_mcp.audit import AuditLog
from tossinvest_mcp.config import Settings
from tossinvest_mcp.paper import PaperBroker
from tossinvest_mcp.safety import SafetyManager
from tossinvest_mcp.tools import AppContext

KST = ZoneInfo("Asia/Seoul")


class FakeClient:
    """Mimics the subset of TossInvestClient that the MCP tools call. Records calls."""

    def __init__(self):
        self.calls = []
        self.place_payloads = []

    # market data
    def get_prices(self, symbols):
        self.calls.append(("get_prices", symbols))
        return [Price.model_validate({"symbol": s, "lastPrice": "70000", "currency": "KRW"}) for s in symbols]

    def get_orderbook(self, symbol):
        self.calls.append(("get_orderbook", symbol))
        return {"symbol": symbol, "asks": [], "bids": []}

    def get_trades(self, symbol, count=50):
        self.calls.append(("get_trades", symbol))
        return [{"price": "70000", "volume": "1"}]

    def get_candles(self, symbol, interval, count=100, before=None):
        self.calls.append(("get_candles", symbol, interval))
        return {"candles": [], "nextBefore": None}

    def get_stocks(self, symbols):
        self.calls.append(("get_stocks", symbols))
        return [{"symbol": s, "name": "삼성전자"} for s in symbols]

    def get_exchange_rate(self, base, quote):
        self.calls.append(("get_exchange_rate", base, quote))
        return {"baseCurrency": base, "quoteCurrency": quote, "rate": "1350"}

    def get_market_calendar(self, country, date=None):
        self.calls.append(("get_market_calendar", country))
        return {"today": {"integrated": {"regularMarket": {"startTime": "09:00", "endTime": "15:30"}}}}

    # account / order (real-mode path)
    def get_accounts(self):
        self.calls.append(("get_accounts",))
        return [Account.model_validate({"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"})]

    def get_holdings(self, symbol=None):
        self.calls.append(("get_holdings", symbol))
        return {"items": [], "marketValue": {"krw": "0"}}

    def get_buying_power(self, currency):
        self.calls.append(("get_buying_power", currency))
        return BuyingPower.model_validate({"currency": currency, "cashBuyingPower": "500000"})

    def get_sellable_quantity(self, symbol):
        self.calls.append(("get_sellable_quantity", symbol))
        return {"sellableQuantity": "0"}

    def get_commissions(self):
        self.calls.append(("get_commissions",))
        return [{"marketCountry": "KR", "commissionRate": "0.015"}]

    def list_orders(self, status="OPEN", symbol=None, cursor=None, limit=20):
        self.calls.append(("list_orders", status, symbol))
        return {"orders": [], "hasNext": False}

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return {"orderId": order_id, "status": "PENDING"}

    def place_order(self, **kwargs):
        from pytossinvest.models import OrderResponse
        self.place_payloads.append(kwargs)
        return OrderResponse.model_validate({"orderId": "real-1", "clientOrderId": kwargs.get("client_order_id")})

    def modify_order(self, order_id, **kwargs):
        self.calls.append(("modify_order", order_id, kwargs))
        return {"orderId": "real-2"}

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        return {"orderId": "real-3"}


@pytest.fixture
def fake_client():
    return FakeClient()


def make_app(fake_client, tmp_path, *, mode="paper", now_kst=None, **settings_kw):
    settings = Settings(_env_file=None, mode=mode,
                        audit_log_path=str(tmp_path / "audit.log"), **settings_kw)
    paper = PaperBroker(starting_cash=settings.paper_starting_cash, next_id=_counter("paper"))
    safety = SafetyManager(settings, now=lambda: 1000.0, today=lambda: date(2026, 6, 17),
                           gen_id=_counter("cli"))
    audit = AuditLog(settings.audit_log_path)
    return AppContext(
        config=settings, client=fake_client, paper=paper, safety=safety, audit=audit,
        now_kst=now_kst or (lambda: datetime(2026, 6, 17, 10, 0, tzinfo=KST)),
    )


def _counter(prefix):
    state = {"i": 0}
    def gen():
        state["i"] += 1
        return f"{prefix}-{state['i']}"
    return gen


@pytest.fixture
def app_factory(fake_client, tmp_path):
    def factory(**kw):
        return make_app(fake_client, tmp_path, **kw)
    return factory
```

- [ ] **Step 2: Write the failing test `test_tools_read.py`**

```python
import tossinvest_mcp.tools as T


def test_get_quote_single_symbol_includes_depth(app_factory, fake_client):
    app = app_factory(mode="read_only")
    out = T.get_quote(app, ["005930"])
    assert out["prices"][0] == {"symbol": "005930", "lastPrice": "70000", "currency": "KRW"}
    assert "orderbook" in out and "trades" in out


def test_get_quote_multi_symbol_no_depth(app_factory):
    app = app_factory(mode="read_only")
    out = T.get_quote(app, ["005930", "000660"])
    assert len(out["prices"]) == 2
    assert "orderbook" not in out


def test_get_accounts_paper_is_synthetic(app_factory):
    app = app_factory(mode="paper")
    out = T.get_accounts(app)
    assert out["accounts"][0]["accountType"] == "PAPER"


def test_get_accounts_real_in_read_only(app_factory, fake_client):
    app = app_factory(mode="read_only")
    out = T.get_accounts(app)
    assert out["accounts"][0]["accountSeq"] == 7
    assert ("get_accounts",) in fake_client.calls


def test_get_holdings_paper_uses_broker(app_factory):
    app = app_factory(mode="paper")
    app.paper.place(symbol="005930", side="BUY", order_type="LIMIT", fill_price="70000", quantity="2")
    out = T.get_holdings(app)
    assert out["items"][0]["symbol"] == "005930"
    assert out["cash"] == "9860000"  # 10,000,000 - 140,000


def test_get_holdings_real_in_read_only(app_factory, fake_client):
    app = app_factory(mode="read_only")
    T.get_holdings(app)
    assert ("get_holdings", None) in fake_client.calls


def test_get_market_info_calendar_and_optional_fx(app_factory):
    app = app_factory(mode="read_only")
    out = T.get_market_info(app, "KR", base_currency="USD", quote_currency="KRW")
    assert "calendar" in out and "exchangeRate" in out


def test_get_order_paper_not_found_raises(app_factory):
    app = app_factory(mode="paper")
    import pytest
    with pytest.raises(ValueError):
        T.get_order(app, "nope")
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_read.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tossinvest_mcp.tools'`.

- [ ] **Step 4: Implement `tools.py` (AppContext + read tools + shared helpers)**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .config import Settings
from .paper import PaperBroker, PaperOrder
from .safety import SafetyManager

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class AppContext:
    config: Settings
    client: Any  # TossInvestClient or compatible
    paper: PaperBroker
    safety: SafetyManager
    audit: AuditLog
    now_kst: Callable[[], datetime] = lambda: datetime.now(_KST)

    @property
    def use_paper(self) -> bool:
        return self.config.use_paper

    @property
    def is_live(self) -> bool:
        return self.config.is_live


def _paper_order_dict(o: PaperOrder) -> dict:
    return {
        "orderId": o.order_id,
        "symbol": o.symbol,
        "side": o.side,
        "orderType": o.order_type,
        "quantity": str(o.quantity),
        "price": str(o.price),
        "status": o.status,
        "clientOrderId": o.client_order_id,
    }


# --- market data (always the real client) ---

def get_quote(app: AppContext, symbols: list[str]) -> dict:
    prices = app.client.get_prices(symbols)
    out: dict = {
        "prices": [
            {"symbol": p.symbol, "lastPrice": str(p.last_price), "currency": p.currency}
            for p in prices
        ]
    }
    if len(symbols) == 1:
        out["orderbook"] = app.client.get_orderbook(symbols[0])
        out["trades"] = app.client.get_trades(symbols[0])
    return out


def get_candles(app: AppContext, symbol: str, interval: str,
                count: int = 100, before: "str | None" = None) -> dict:
    return app.client.get_candles(symbol, interval, count=count, before=before)


def get_stock_info(app: AppContext, symbols: list[str]) -> dict:
    return {"stocks": app.client.get_stocks(symbols)}


def get_market_info(app: AppContext, country: str = "KR",
                    base_currency: "str | None" = None,
                    quote_currency: "str | None" = None) -> dict:
    out: dict = {"calendar": app.client.get_market_calendar(country)}
    if base_currency and quote_currency:
        out["exchangeRate"] = app.client.get_exchange_rate(base_currency, quote_currency)
    return out


# --- account / order reads (paper-routed) ---

def get_accounts(app: AppContext) -> dict:
    if app.use_paper:
        return {"accounts": [{"accountNo": "PAPER", "accountSeq": 0, "accountType": "PAPER"}]}
    return {"accounts": [a.model_dump(by_alias=True) for a in app.client.get_accounts()]}


def get_holdings(app: AppContext, symbol: "str | None" = None) -> dict:
    if app.use_paper:
        return app.paper.holdings()
    return app.client.get_holdings(symbol)


def list_orders(app: AppContext, status: str = "OPEN", symbol: "str | None" = None) -> dict:
    if app.use_paper:
        items = [_paper_order_dict(o) for o in app.paper.list_orders()
                 if symbol is None or o.symbol == symbol]
        return {"orders": items, "hasNext": False}
    return app.client.list_orders(status=status, symbol=symbol)


def get_order(app: AppContext, order_id: str) -> dict:
    if app.use_paper:
        o = app.paper.get_order(order_id)
        if o is None:
            raise ValueError(f"paper order not found: {order_id}")
        return _paper_order_dict(o)
    return app.client.get_order(order_id)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_read.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/tests/conftest.py tossinvest-mcp/tests/test_tools_read.py
git commit -m "feat(mcp): AppContext + read tools (paper-routed account reads, real market data)"
```

---

## Task 9: `tools.py` — write tools (readiness, preview→place, modify/cancel)

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/tools.py` (add write tools + helpers)
- Test: `tossinvest-mcp/tests/test_tools_write.py`

Write tools wire the guardrails + token lifecycle from `safety.py`, route fills to the paper broker (paper) or `client.place_order` (live), and append to the audit log. `modify`/`cancel` are live-only.

- [ ] **Step 1: Write the failing test**

```python
import json

import pytest

import tossinvest_mcp.tools as T
from tossinvest_mcp.safety import GuardrailError
from tossinvest_mcp.paper import PaperError


def test_get_order_readiness_paper(app_factory):
    app = app_factory(mode="paper")
    out = T.get_order_readiness(app, "005930")
    assert out["buyingPower"] == "10000000"
    assert out["sellableQuantity"] == "0"


def test_preview_returns_token_and_estimate(app_factory):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    assert pv["estimatedNotional"] == "700000"
    assert pv["confirmationToken"]
    assert pv["clientOrderId"]


def test_preview_rejected_by_guardrail(app_factory):
    app = app_factory(mode="paper", max_order_amount="100000")
    with pytest.raises(GuardrailError) as e:
        T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="10", price="70000")  # 700,000 > 100,000
    assert e.value.code == "order-amount-cap"


def test_place_requires_valid_token(app_factory):
    app = app_factory(mode="paper")
    with pytest.raises(GuardrailError) as e:
        T.place_order(app, confirmation_token="bogus")
    assert e.value.code == "invalid-confirmation"


def test_preview_then_place_fills_paper_and_audits(app_factory):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    out = T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert out["status"] == "FILLED"
    assert app.paper.cash.__str__() == "9300000"  # 10,000,000 - 700,000
    # token now consumed -> second place fails
    with pytest.raises(GuardrailError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    # audit log has preview + place lines
    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    tools = [json.loads(l)["tool"] for l in lines]
    assert tools == ["preview_order", "place_order"]


def test_place_live_calls_client_with_string_price(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    out = T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert out["orderId"] == "real-1"
    sent = fake_client.place_payloads[-1]
    assert sent["price"] == "70000"            # string, not number
    assert sent["client_order_id"] == pv["clientOrderId"]


def test_modify_and_cancel_are_live_only(app_factory):
    app = app_factory(mode="paper")
    with pytest.raises(PaperError):
        T.modify_order(app, "paper-1", order_type="LIMIT", price="71000")
    with pytest.raises(PaperError):
        T.cancel_order(app, "paper-1")


def test_cancel_live_calls_client(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True)
    out = T.cancel_order(app, "real-1")
    assert out["orderId"] == "real-3"
    assert ("cancel_order", "real-1") in fake_client.calls


def test_place_market_paper_no_ref_price_errors_without_corruption(app_factory, fake_client):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="MARKET", quantity="10")
    # market data momentarily unavailable at place time (it was available at preview)
    fake_client.get_prices = lambda symbols: []
    with pytest.raises(PaperError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    # no corrupt zero-price fill happened
    assert app.paper.positions == {}
    assert str(app.paper.cash) == "10000000"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_write.py -v`
Expected: FAIL with `AttributeError: module 'tossinvest_mcp.tools' has no attribute 'get_order_readiness'`.

- [ ] **Step 3: Add write tools + helpers to `tools.py`** (append at end of file)

```python
from decimal import Decimal

from pytossinvest.money import to_decimal
from . import market_hours


def _market_gate(app: AppContext, symbol: str) -> "tuple[bool, bool]":
    """Return (is_market_open, enforce_hours). Hours are enforced only in live mode."""
    enforce = app.config.enforce_market_hours and app.is_live
    if not enforce:
        return True, False
    country = "US" if symbol.isalpha() else "KR"
    cal = app.client.get_market_calendar(country)
    return market_hours.is_market_open(cal, app.now_kst(), country), True


def _ref_price(app: AppContext, symbol: str) -> "str | None":
    prices = app.client.get_prices([symbol])
    return str(prices[0].last_price) if prices else None


def get_order_readiness(app: AppContext, symbol: str, side: str = "BUY",
                        currency: str = "KRW") -> dict:
    if app.use_paper:
        return {
            "buyingPower": str(app.paper.buying_power()),
            "sellableQuantity": str(app.paper.sellable_quantity(symbol)),
            "commissions": [],
        }
    bp = app.client.get_buying_power(currency)
    return {
        "buyingPower": {"currency": bp.currency, "cashBuyingPower": str(bp.cash_buying_power)},
        "sellableQuantity": app.client.get_sellable_quantity(symbol),
        "commissions": app.client.get_commissions(),
    }


def preview_order(app: AppContext, *, symbol: str, side: str, order_type: str,
                  quantity: "str | None" = None, price: "str | None" = None,
                  order_amount: "str | None" = None, time_in_force: str = "DAY",
                  confirm_high_value_order: bool = False) -> dict:
    ref = None
    if order_type == "MARKET" and order_amount is None:
        ref = _ref_price(app, symbol)
    spec = app.safety.build_spec(
        symbol=symbol, side=side, order_type=order_type, quantity=quantity, price=price,
        order_amount=order_amount, time_in_force=time_in_force,
        confirm_high_value_order=confirm_high_value_order, ref_price=ref,
    )
    is_open, enforce = _market_gate(app, symbol)
    app.safety.check_guardrails(spec, is_market_open=is_open, enforce_hours=enforce)
    token = app.safety.issue_token(spec)
    app.audit.record({
        "tool": "preview_order", "mode": app.config.mode, "decision": "previewed",
        "symbol": symbol, "side": side, "notional": spec.notional,
        "clientOrderId": spec.client_order_id, "token": token,
    })
    return {
        "confirmationToken": token,
        "clientOrderId": spec.client_order_id,
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "estimatedNotional": str(spec.notional),
        "expiresInSec": app.config.confirmation_ttl_sec,
        "mode": app.config.mode,
    }


def place_order(app: AppContext, *, confirmation_token: str) -> dict:
    spec = app.safety.consume(confirmation_token)  # validates exists + not expired
    try:
        if app.use_paper:
            if spec.price is not None:
                fill_price = spec.price
                qty = spec.quantity
            else:
                from .paper import PaperError
                fill_price = _ref_price(app, spec.symbol)
                if not fill_price or to_decimal(fill_price) <= 0:
                    raise PaperError(
                        f"no reference price available for MARKET fill of {spec.symbol}; retry"
                    )
                if spec.quantity is not None:
                    qty = spec.quantity
                else:  # US amount-based: qty = amount / price
                    qty = str(to_decimal(spec.order_amount) / to_decimal(fill_price))
            order = app.paper.place(
                symbol=spec.symbol, side=spec.side, order_type=spec.order_type,
                fill_price=fill_price, quantity=qty, client_order_id=spec.client_order_id,
            )
            result = _paper_order_dict(order)
        else:
            resp = app.client.place_order(
                symbol=spec.symbol, side=spec.side, order_type=spec.order_type,
                quantity=spec.quantity, price=spec.price, order_amount=spec.order_amount,
                time_in_force=spec.time_in_force, client_order_id=spec.client_order_id,
                confirm_high_value_order=spec.confirm_high_value_order,
            )
            result = {"orderId": resp.order_id, "clientOrderId": resp.client_order_id}
    except Exception as e:
        app.audit.record({
            "tool": "place_order", "mode": app.config.mode, "decision": "error",
            "error": str(e), "clientOrderId": spec.client_order_id,
        })
        raise  # token NOT finalized -> retry reuses same clientOrderId (idempotent)

    app.safety.finalize(confirmation_token, spec.notional)
    app.audit.record({
        "tool": "place_order", "mode": app.config.mode, "decision": "placed",
        "result": result, "clientOrderId": spec.client_order_id,
    })
    return result


def modify_order(app: AppContext, order_id: str, *, order_type: str,
                 price: "str | None" = None, quantity: "str | None" = None,
                 confirm_high_value_order: bool = False) -> dict:
    if app.use_paper:
        from .paper import PaperError
        raise PaperError("paper mode fills orders immediately; modify is live-only")
    result = app.client.modify_order(
        order_id, order_type=order_type, price=price, quantity=quantity,
        confirm_high_value_order=confirm_high_value_order,
    )
    app.audit.record({"tool": "modify_order", "mode": app.config.mode,
                      "decision": "modified", "orderId": order_id, "result": result})
    return result


def cancel_order(app: AppContext, order_id: str) -> dict:
    if app.use_paper:
        from .paper import PaperError
        raise PaperError("paper mode fills orders immediately; cancel is live-only")
    result = app.client.cancel_order(order_id)
    app.audit.record({"tool": "cancel_order", "mode": app.config.mode,
                      "decision": "canceled", "orderId": order_id, "result": result})
    return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_write.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/tests/test_tools_write.py
git commit -m "feat(mcp): write tools (readiness, preview->place, modify/cancel) with audit + idempotency"
```

---

## Task 10: `server.py` — FastMCP registration by mode + entrypoint

**Files:**
- Create: `tossinvest-mcp/src/tossinvest_mcp/server.py`
- Test: `tossinvest-mcp/tests/test_server_modes.py`

`build_server` constructs the `AppContext`, always registers the 8 read tools, and registers the 5 write tools only when `mode != "read_only"`. Tests assert the registered tool **names** per mode via `await mcp.list_tools()` (driven by `asyncio.run`), plus one in-process `call_tool` smoke.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_server_modes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tossinvest_mcp.server'`.

- [ ] **Step 3: Implement `server.py`**

```python
from __future__ import annotations

import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from .audit import AuditLog
from .config import Settings
from .paper import PaperBroker
from .safety import SafetyManager
from .tools import AppContext
from . import tools as T

_KST = ZoneInfo("Asia/Seoul")


def build_app_context(settings: Settings, *, client) -> AppContext:
    paper = PaperBroker(starting_cash=settings.paper_starting_cash)
    safety = SafetyManager(
        settings,
        now=_time.monotonic,
        today=lambda: datetime.now(_KST).date(),
    )
    audit = AuditLog(settings.audit_log_path)
    return AppContext(
        config=settings, client=client, paper=paper, safety=safety, audit=audit,
        now_kst=lambda: datetime.now(_KST),
    )


def build_server(settings: Settings, *, client) -> FastMCP:
    app = build_app_context(settings, client=client)
    mcp = FastMCP("tossinvest-mcp")
    _register_reads(mcp, app)
    if settings.mode != "read_only":
        _register_writes(mcp, app)
    return mcp


def _register_reads(mcp: FastMCP, app: AppContext) -> None:
    @mcp.tool(name="get_accounts",
              description="List brokerage accounts. Paper mode returns a synthetic PAPER account.")
    def get_accounts() -> dict:
        return T.get_accounts(app)

    @mcp.tool(name="get_holdings",
              description="Current holdings/positions. Money & quantities are strings.")
    def get_holdings(symbol: "str | None" = None) -> dict:
        return T.get_holdings(app, symbol)

    @mcp.tool(name="get_quote",
              description="Latest price(s) for up to 200 symbols; a single symbol also returns "
                          "orderbook & recent trades. All prices are strings.")
    def get_quote(symbols: list[str]) -> dict:
        return T.get_quote(app, symbols)

    @mcp.tool(name="get_candles", description="OHLC candles. interval is '1m' or '1d'.")
    def get_candles(symbol: str, interval: str, count: int = 100,
                    before: "str | None" = None) -> dict:
        return T.get_candles(app, symbol, interval, count, before)

    @mcp.tool(name="get_stock_info", description="Basic stock info for up to 200 symbols.")
    def get_stock_info(symbols: list[str]) -> dict:
        return T.get_stock_info(app, symbols)

    @mcp.tool(name="get_market_info",
              description="Market calendar for a country ('KR'/'US'); optional FX rate when "
                          "base_currency & quote_currency are given.")
    def get_market_info(country: str = "KR", base_currency: "str | None" = None,
                        quote_currency: "str | None" = None) -> dict:
        return T.get_market_info(app, country, base_currency, quote_currency)

    @mcp.tool(name="list_orders",
              description="Open orders (real API returns OPEN only). Paper returns simulated orders.")
    def list_orders(status: str = "OPEN", symbol: "str | None" = None) -> dict:
        return T.list_orders(app, status, symbol)

    @mcp.tool(name="get_order", description="Order detail by id.")
    def get_order(order_id: str) -> dict:
        return T.get_order(app, order_id)


def _register_writes(mcp: FastMCP, app: AppContext) -> None:
    @mcp.tool(name="get_order_readiness",
              description="Buying power, sellable quantity, and commissions before ordering.")
    def get_order_readiness(symbol: str, side: str = "BUY", currency: str = "KRW") -> dict:
        return T.get_order_readiness(app, symbol, side, currency)

    @mcp.tool(name="preview_order",
              description="STEP 1 of 2. Validate an order against guardrails and estimate cost; "
                          "returns a confirmation_token. Money/quantity are strings. Does NOT place "
                          "the order. For a MARKET quantity order, the current price is used to estimate.")
    def preview_order(symbol: str, side: str, order_type: str, quantity: "str | None" = None,
                      price: "str | None" = None, order_amount: "str | None" = None,
                      time_in_force: str = "DAY", confirm_high_value_order: bool = False) -> dict:
        return T.preview_order(
            app, symbol=symbol, side=side, order_type=order_type, quantity=quantity,
            price=price, order_amount=order_amount, time_in_force=time_in_force,
            confirm_high_value_order=confirm_high_value_order,
        )

    @mcp.tool(name="place_order",
              description="STEP 2 of 2. Place the order previously validated by preview_order, using "
                          "its confirmation_token. Idempotent: a failed attempt can be retried with the "
                          "same token.")
    def place_order(confirmation_token: str) -> dict:
        return T.place_order(app, confirmation_token=confirmation_token)

    @mcp.tool(name="modify_order",
              description="Modify an open order (live only; returns a NEW orderId). US orders: price only.")
    def modify_order(order_id: str, order_type: str, price: "str | None" = None,
                     quantity: "str | None" = None, confirm_high_value_order: bool = False) -> dict:
        return T.modify_order(app, order_id, order_type=order_type, price=price,
                              quantity=quantity, confirm_high_value_order=confirm_high_value_order)

    @mcp.tool(name="cancel_order",
              description="Cancel an open order (live only; returns a NEW orderId).")
    def cancel_order(order_id: str) -> dict:
        return T.cancel_order(app, order_id)


def main() -> None:
    settings = Settings()
    from pytossinvest import TossInvestClient

    client = TossInvestClient(
        settings.client_id, settings.client_secret, base_url=settings.base_url
    )
    mcp = build_server(settings, client=client)
    mcp.run()  # stdio transport (default) for MCP clients like Claude Desktop
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_server_modes.py -v`
Expected: 4 passed. (If `call_tool`'s return shape differs across `mcp` versions, the smoke test only asserts non-None — adjust nothing else.)

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/server.py tossinvest-mcp/tests/test_server_modes.py
git commit -m "feat(mcp): FastMCP server with mode-based tool registration + stdio entrypoint"
```

---

## Task 11: Full suite green + README usage + manual server smoke

**Files:**
- Modify: `tossinvest-mcp/README.md`
- Test: all

- [ ] **Step 1: Run the full MCP suite**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -v`
Expected: all pass (smoke 2 + config 6 + audit 3 + paper 7 + market_hours 6 + safety_guardrails 13 + safety_tokens 5 + tools_read 8 + tools_write 8 + server_modes 4).

- [ ] **Step 2: Run the SDK suite too (confirm the workspace dep didn't break anything)**

Run: `uv run --package pytossinvest pytest pytossinvest/tests -q`
Expected: all SDK tests still pass (unchanged).

- [ ] **Step 3: Manual server smoke in paper mode (no network needed for startup)**

Run:
```bash
cd /Users/cyj/workspace/personal/toss
TOSSINVEST_MODE=paper TOSSINVEST_CLIENT_ID=demo TOSSINVEST_CLIENT_SECRET=demo \
  uv run --package tossinvest-mcp python -c "from tossinvest_mcp.config import Settings; from tossinvest_mcp.server import build_server; from tossinvest_mcp.tools import AppContext; import asyncio; from pytossinvest import TossInvestClient; mcp=build_server(Settings(), client=TossInvestClient('demo','demo')); print(sorted(t.name for t in asyncio.run(mcp.list_tools())))"
```
Expected: prints the 13 tool names (8 read + 5 write). This proves the real entrypoint wiring (Settings → real client → FastMCP) builds without error.

- [ ] **Step 4: Add a usage section to `README.md`** (append)

````markdown
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
````

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/README.md
git commit -m "docs(mcp): README tools, config, Claude Desktop setup, two-step ordering"
```

---

## Self-Review (completed)

**Spec coverage:**
- §3.1 modes (read_only/paper/live + `allow_live` double gate) → Task 2 (config validator) + Task 10 (registration skips writes in read_only; routing via `use_paper`/`is_live`). ✓
- §3.2 guardrails (per-order cap, daily cap, allow/deny, 1억 confirm, 30억 reject, hours) → Task 6. Hours live-only via `_market_gate` (Task 9). ✓
- §3.3 two-step preview→place with bound token → Tasks 7 (token lifecycle) + 9 (tools). ✓
- §3.4 idempotency (auto `clientOrderId`, consume-on-success so retries reuse) → Tasks 7 + 9 (`finalize` only on success; failure re-raises with token pending). ✓
- §3.5 audit log → Task 3 + recorded in Task 9 write tools. ✓
- §4 tool list (~12): 8 reads + `get_order_readiness` + `preview_order`/`place_order`/`modify_order`/`cancel_order` = 13 → Tasks 8–10. Endpoint consolidation (`get_quote` = prices+orderbook+trades; `get_market_info` = calendar+fx) matches §4. ✓
- §3.1 paper engine (sim portfolio) → Task 4. ✓
- config.py (pydantic-settings, env) → Task 2; server.py (mode-based registration) → Task 10. ✓
- §6 tests (mode registration incl. read_only write-absent; paper buy→holdings→sell→pnl; preview/confirm binding; guardrail rejection; no live keys) → Tasks 4/6/7/8/9/10. ✓
- §9 license (MCP = Apache-2.0, root keeps SDK MIT) → Task 1 (LICENSE/NOTICE + `license` field). ✓

**Deliberate scope bounds (documented, not gaps):** paper `modify`/`cancel` are live-only (paper fills instantly); market-hours wrap-past-midnight treated as same-day window (live-only, overridable); guardrail notional compared in the order's own currency (no FX conversion); `modify_order` in live is not preview-gated. Each is noted inline where it occurs.

**Placeholder scan:** every code step contains complete code. The only "paste standard text" item is the Apache-2.0 LICENSE (Task 1 Step 3) — a fixed canonical document, not an implementation placeholder.

**Type consistency:** `AppContext` fields (`config/client/paper/safety/audit/now_kst`) and `.use_paper`/`.is_live` properties are identical across Tasks 8–10. `SafetyManager(config, *, now, today, gen_id=None)` and methods (`build_spec`, `check_guardrails`, `record_spend`, `issue_token`, `consume`, `finalize`) match between Tasks 6, 7, 9, and `conftest.py`. `PaperBroker(starting_cash=, next_id=)` + `.place(symbol, side, order_type, fill_price, quantity, client_order_id=)` consistent across Tasks 4, 8, 9. Tool function signatures in `tools.py` (Tasks 8–9) match the closures in `server.py` (Task 10). `GuardrailError.code`/`PaperError` used consistently in tests and tools.

**SDK boundary:** the MCP layer only imports `pytossinvest` (`TossInvestClient`, `models`, `money.to_decimal`); no SDK file is modified. ✓
