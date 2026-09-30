# Conditional orders over MCP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Toss conditional orders (SINGLE / OCO / OTO) through the MCP server: read them in every real-account mode, and create / modify / cancel them in `live` behind the existing guardrail + confirmation-token + daily-cap path.

**Architecture:** A new `conditional.py` validates flat tool arguments into SDK kwargs, prices a condition group, and holds the conditional tool functions. Preview tools issue confirmation tokens whose `OrderSpec` has `kind="conditional"`; the existing `tools.place_order` / `tools.modify_order` execute them, branching on `kind` only at the execution step, so consume → guardrails → reserve → execute → commit / release stays one path.

**Tech Stack:** Python 3.12, uv workspace, `mcp` 2.2.0 (`MCPServer`), `pytossinvest` SDK (already has `create/modify/cancel/list/get_conditional_order`), pytest, fakeredis.

**Spec:** `docs/superpowers/specs/2026-09-30-conditional-orders-mcp-design.md`

## Global Constraints

- Money and quantities stay strings / `Decimal` end to end; never `float`.
- `place_order` / `modify_order` must keep: consume → `check_guardrails(check_daily=False)` → `reserve` → execute → `commit`, or `_settle_failure` on error. No other path issues or consumes tokens.
- Daily cap: conditional create reserves the full notional on the registration day; modify reserves `new − old` (old unknown → full new); cancel never refunds.
- Notional: leg price = `orderPrice` (LIMIT) or `triggerPrice` (MARKET); SINGLE = first leg, OCO = max of legs, OTO = sum of legs.
- Market-hours gate is skipped for conditional previews (`enforce_hours=False`).
- `paper`: conditional writes raise `PaperError`; list returns `{"conditionalOrders": [], "hasNext": False}`; get raises `ValueError`.
- Tools register through `mcp_tool` (never `@mcp.tool`) with `Literal` enums; in `server.py` do not wrap aliases in quotes (module uses `from __future__ import annotations`).
- Commit messages and docs never mention AI authorship (repo rule).
- Test commands: MCP `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`; SDK `uv run --package pytossinvest --extra dev pytest pytossinvest/tests`. Run from the repo root `/Users/cyj/workspace/personal/toss`.

---

## File Structure

- Create `pytossinvest-mcp/src/pytossinvest_mcp/conditional.py` — request validation (`build_request`), notional (`group_notional`, `request_notional`, `detail_notional`), tool functions (`preview_conditional_order`, `preview_conditional_modify`, `cancel_conditional_order`, `list_conditional_orders`, `get_conditional_order`) and executors (`execute_place`, `execute_modify`).
- Modify `pytossinvest-mcp/src/pytossinvest_mcp/safety.py` — `ORDER_KIND` / `CONDITIONAL_KIND`, `OrderSpec.kind` / `.conditional`, `SafetyManager.build_conditional_spec`.
- Modify `pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py` — serialize `kind` / `conditional` with defaults for old tokens.
- Modify `pytossinvest-mcp/src/pytossinvest_mcp/tools.py` — `place_order` / `modify_order` dispatch on `kind`; audit `kind`.
- Modify `pytossinvest-mcp/src/pytossinvest_mcp/server.py` — register 2 reads + 3 writes.
- Modify `pytossinvest-mcp/tests/conftest.py` — `FakeClient` conditional methods.
- Create `pytossinvest-mcp/tests/test_conditional_request.py` (Task 1), `pytossinvest-mcp/tests/test_conditional_tools.py` (Task 3).
- Modify `pytossinvest-mcp/tests/test_stores_redis.py` (Task 2), `pytossinvest-mcp/tests/test_server_modes.py` (Task 4).
- Docs (Task 5): `AGENTS.md`, `docs/wiki/pytossinvest-mcp.md`, `pytossinvest-mcp/README.md`, `README.md`, `docs/wiki/tossinvest-open-api.md`.

---

### Task 1: Request validation and notional (pure functions)

**Files:**
- Create: `pytossinvest-mcp/src/pytossinvest_mcp/conditional.py`
- Test: `pytossinvest-mcp/tests/test_conditional_request.py`

**Interfaces:**
- Consumes: `safety.GuardrailError(code, message)`, `safety._is_positive_number(val: str) -> bool`, `pytossinvest.money.to_decimal`.
- Produces:
  - `build_request(*, type, quantity, order_type, expire_date, first_side, first_trigger_price, first_order_price=None, second_side=None, second_trigger_price=None, second_order_price=None) -> dict` returning exactly `{"type", "quantity", "order_type", "expire_date", "first", "second"}` where `first`/`second` are wire dicts `{"orderSide", "triggerPrice", "orderPrice"?}` and `second` may be `None`. These are the SDK `create_conditional_order` kwargs minus `symbol` / `client_order_id` / `confirm_high_value_order`.
  - `group_notional(group_type: str, quantity: str, order_type: str, first: dict, second: dict | None) -> Decimal | None`
  - `request_notional(request: dict) -> Decimal`
  - `detail_notional(detail: dict) -> Decimal | None` (API detail shape: `type`, `quantity`, `orderType`, `first`, `second?`).

- [ ] **Step 1: Write the failing tests**

Create `pytossinvest-mcp/tests/test_conditional_request.py`:

```python
from decimal import Decimal

import pytest

from pytossinvest_mcp.conditional import build_request, detail_notional, request_notional
from pytossinvest_mcp.safety import GuardrailError

BASE = dict(type="SINGLE", quantity="10", order_type="LIMIT", expire_date="2026-07-31",
            first_side="SELL", first_trigger_price="65000", first_order_price="64900")
OCO = dict(BASE, type="OCO", first_trigger_price="80000", first_order_price="79900",
           second_side="SELL", second_trigger_price="65000", second_order_price="64900")
OTO = dict(BASE, type="OTO", first_side="BUY", first_trigger_price="60000", first_order_price="60100",
           second_side="SELL", second_trigger_price="70000", second_order_price="69900")


def test_build_request_returns_sdk_kwargs():
    assert build_request(**BASE) == {
        "type": "SINGLE", "quantity": "10", "order_type": "LIMIT", "expire_date": "2026-07-31",
        "first": {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "64900"},
        "second": None,
    }


def test_market_legs_carry_no_order_price():
    req = build_request(**dict(BASE, order_type="MARKET", first_order_price=None))
    assert req["first"] == {"orderSide": "SELL", "triggerPrice": "65000"}


@pytest.mark.parametrize("kw, expected", [
    (BASE, "649000"),                                                 # SINGLE LIMIT: 10 x orderPrice
    (dict(BASE, order_type="MARKET", first_order_price=None), "650000"),  # MARKET: 10 x triggerPrice
    (OCO, "799000"),                                                  # OCO: the larger leg
    (OTO, "1300000"),                                                 # OTO: both legs
])
def test_request_notional(kw, expected):
    assert request_notional(build_request(**kw)) == Decimal(expected)


@pytest.mark.parametrize("kw, code", [
    (dict(BASE, quantity="ten"), "invalid-order-value"),
    (dict(BASE, first_trigger_price="NaN"), "invalid-order-value"),
    (dict(BASE, first_order_price="0"), "invalid-order-value"),
    (dict(BASE, expire_date="31/07/2026"), "invalid-order-params"),
    (dict(BASE, second_side="SELL", second_trigger_price="60000", second_order_price="59900"),
     "invalid-order-params"),                                       # SINGLE with a second leg
    (dict(BASE, type="OCO"), "invalid-order-params"),               # OCO without a second leg
    (dict(BASE, first_order_price=None), "invalid-order-params"),   # LIMIT without order price
    (dict(BASE, order_type="MARKET"), "invalid-order-params"),      # MARKET with an order price
])
def test_build_request_rejects_readably(kw, code):
    with pytest.raises(GuardrailError) as e:
        build_request(**kw)
    assert e.value.code == code


def test_detail_notional_prices_the_api_shape():
    detail = {"type": "SINGLE", "quantity": "10", "orderType": "LIMIT",
              "first": {"type": "STOP", "status": "WATCHING", "triggerPrice": "65000",
                        "orderPrice": "64900"}}
    assert detail_notional(detail) == Decimal("649000")


def test_detail_notional_is_unknown_without_prices():
    detail = {"type": "SINGLE", "quantity": "10", "orderType": "MARKET",
              "first": {"type": "PROFIT_RATE", "status": "WATCHING", "triggerPrice": None,
                        "targetProfitRate": "10"}}
    assert detail_notional(detail) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_conditional_request.py`
Expected: collection error `ModuleNotFoundError: No module named 'pytossinvest_mcp.conditional'`.

- [ ] **Step 3: Implement**

Create `pytossinvest-mcp/src/pytossinvest_mcp/conditional.py`:

```python
"""Conditional orders (SINGLE / OCO / OTO) over MCP.

Preview tools here issue confirmation tokens whose spec has kind=conditional; placing and
modifying run through tools.place_order / tools.modify_order so the safety path stays single.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pytossinvest.money import to_decimal

from .safety import GuardrailError, _is_positive_number

LIMIT = "LIMIT"
SINGLE = "SINGLE"
OCO = "OCO"


def _leg(side: str, trigger_price: str, order_price: str | None) -> dict:
    leg = {"orderSide": side, "triggerPrice": trigger_price}
    if order_price is not None:
        leg["orderPrice"] = order_price
    return leg


def build_request(*, type: str, quantity: str, order_type: str, expire_date: str,
                  first_side: str, first_trigger_price: str, first_order_price: str | None = None,
                  second_side: str | None = None, second_trigger_price: str | None = None,
                  second_order_price: str | None = None) -> dict:
    """Validate flat tool arguments into SDK create/modify kwargs (without symbol)."""
    numbers = {"quantity": quantity, "first_trigger_price": first_trigger_price,
               "first_order_price": first_order_price, "second_trigger_price": second_trigger_price,
               "second_order_price": second_order_price}
    for label, val in numbers.items():
        if val is not None and not _is_positive_number(val):
            raise GuardrailError("invalid-order-value", f"{label} must be a positive number, got {val!r}")

    try:
        date.fromisoformat(expire_date)
    except ValueError:
        raise GuardrailError("invalid-order-params",
                             f"expire_date must be YYYY-MM-DD, got {expire_date!r}") from None

    has_second = any(v is not None for v in (second_side, second_trigger_price, second_order_price))
    if type == SINGLE and has_second:
        raise GuardrailError("invalid-order-params", "SINGLE takes no second condition")
    if type != SINGLE and (second_side is None or second_trigger_price is None):
        raise GuardrailError("invalid-order-params",
                             f"{type} needs second_side and second_trigger_price")

    order_prices = [first_order_price, second_order_price] if has_second else [first_order_price]
    if order_type == LIMIT and any(p is None for p in order_prices):
        raise GuardrailError("invalid-order-params", "LIMIT needs an order price for every condition")
    if order_type != LIMIT and any(p is not None for p in order_prices):
        raise GuardrailError("invalid-order-params", "MARKET takes no order price")

    second = _leg(second_side, second_trigger_price, second_order_price) if has_second else None
    return {"type": type, "quantity": quantity, "order_type": order_type, "expire_date": expire_date,
            "first": _leg(first_side, first_trigger_price, first_order_price), "second": second}


def _leg_price(order_type: str, leg: dict) -> Decimal | None:
    value = leg.get("orderPrice" if order_type == LIMIT else "triggerPrice")
    return None if value is None else to_decimal(value)


def group_notional(group_type: str, quantity: str, order_type: str,
                   first: dict, second: dict | None) -> Decimal | None:
    """SINGLE = its one leg, OCO = the larger leg (only one fires), OTO = both legs.
    None when a leg has no price to count (e.g. a PROFIT_RATE condition made in the app)."""
    legs = [first] if second is None else [first, second]
    prices = [_leg_price(order_type, leg) for leg in legs]
    if any(p is None for p in prices):
        return None
    amounts = [to_decimal(quantity) * p for p in prices]
    if group_type == OCO:
        return max(amounts)
    return sum(amounts, Decimal("0"))


def request_notional(request: dict) -> Decimal:
    return group_notional(request["type"], request["quantity"], request["order_type"],
                          request["first"], request["second"])


def detail_notional(detail: dict) -> Decimal | None:
    """Notional of an existing conditional order in the API's detail shape."""
    return group_notional(detail["type"], detail["quantity"], detail["orderType"],
                          detail["first"], detail.get("second"))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_conditional_request.py`
Expected: all pass. Then the full MCP suite: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests` → all pass.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/conditional.py pytossinvest-mcp/tests/test_conditional_request.py
git commit -m "feat(mcp): validate and price conditional order requests"
```

---

### Task 2: Conditional specs in the safety layer and redis tokens

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/safety.py` (`OrderSpec` dataclass ~line 73; `SafetyManager.build_spec` ends ~line 150)
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py:12-45` (`_spec_to_dict`, `_spec_from_dict`)
- Test: `pytossinvest-mcp/tests/test_stores_redis.py` (append)

**Interfaces:**
- Consumes: Task 1 `build_request`, `request_notional`.
- Produces:
  - `safety.ORDER_KIND = "order"`, `safety.CONDITIONAL_KIND = "conditional"`.
  - `OrderSpec.kind: str = ORDER_KIND`, `OrderSpec.conditional: dict | None = None`.
  - `SafetyManager.build_conditional_spec(*, symbol: str, request: dict, notional: Decimal, confirm_high_value_order: bool, currency: str | None, modify_id: str | None = None) -> OrderSpec` (`side` = first leg side, `order_type` / `quantity` from the request, `price` / `order_amount` = None, `time_in_force` = "DAY", fresh `client_order_id`, currency fallback `order_currency(symbol)`, `modify_order_id=modify_id`, `kind=CONDITIONAL_KIND`, `conditional=request`).

- [ ] **Step 1: Write the failing tests**

Append to `pytossinvest-mcp/tests/test_stores_redis.py`:

```python


def _conditional_spec():
    from datetime import date
    from pytossinvest_mcp.conditional import build_request, request_notional
    mgr = SafetyManager(Settings(_env_file=None), now=lambda: 0.0, today=lambda: date(2026, 6, 18),
                        token_store=MemoryTokenStore(), spend_store=MemorySpendStore())
    request = build_request(type="SINGLE", quantity="10", order_type="LIMIT",
                            expire_date="2026-07-31", first_side="SELL",
                            first_trigger_price="65000", first_order_price="64900")
    return mgr.build_conditional_spec(symbol="005930", request=request,
                                      notional=request_notional(request),
                                      confirm_high_value_order=False, currency=None)


def test_conditional_token_roundtrip(r):
    s = RedisTokenStore(r)
    spec = _conditional_spec()
    s.put("t2", spec, expires_at=100.0, issued_at=50.0)
    got, _, _ = s.get("t2")
    assert got.kind == "conditional"
    assert got.conditional == spec.conditional
    assert (got.currency, got.notional, got.side) == ("KRW", Decimal("649000"), "SELL")


def test_token_saved_before_conditional_support_still_loads(r):
    import json
    s = RedisTokenStore(r)
    s.put("t3", _spec(), expires_at=100.0, issued_at=50.0)
    d = json.loads(r.get("tok:t3"))
    del d["spec"]["kind"], d["spec"]["conditional"]   # the pre-upgrade shape
    r.set("tok:t3", json.dumps(d))
    got, _, _ = s.get("t3")
    assert got.kind == "order" and got.conditional is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_stores_redis.py`
Expected: FAIL — `AttributeError: 'SafetyManager' object has no attribute 'build_conditional_spec'` and `KeyError: 'kind'`.

- [ ] **Step 3: Implement**

In `safety.py`, above `@dataclass class OrderSpec`:

```python
ORDER_KIND = "order"
CONDITIONAL_KIND = "conditional"
```

Add two fields at the end of `OrderSpec` (after `prev_notional`):

```python
    kind: str = ORDER_KIND
    conditional: "dict | None" = None  # conditional.build_request() output (SDK kwargs sans symbol)
```

Add to `SafetyManager`, right after `build_spec`:

```python
    def build_conditional_spec(self, *, symbol: str, request: dict, notional: Decimal,
                               confirm_high_value_order: bool, currency: "str | None",
                               modify_id: "str | None" = None) -> OrderSpec:
        """Spec for a conditional order; request comes from conditional.build_request()."""
        return OrderSpec(
            symbol=symbol, side=request["first"]["orderSide"], order_type=request["order_type"],
            quantity=request["quantity"], price=None, order_amount=None, time_in_force="DAY",
            confirm_high_value_order=confirm_high_value_order, notional=notional,
            client_order_id=self._gen_id(),
            currency=currency if currency is not None else order_currency(symbol),
            modify_order_id=modify_id, kind=CONDITIONAL_KIND, conditional=request,
        )
```

In `redis_stores.py`, change the import to `from .safety import ORDER_KIND, OrderSpec`, add to the `_spec_to_dict` return dict:

```python
        "kind": spec.kind,
        "conditional": spec.conditional,
```

and to the `_spec_from_dict` `OrderSpec(...)` call:

```python
        kind=d.get("kind", ORDER_KIND),
        conditional=d.get("conditional"),
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/safety.py pytossinvest-mcp/src/pytossinvest_mcp/redis_stores.py pytossinvest-mcp/tests/test_stores_redis.py
git commit -m "feat(mcp): conditional order specs and their redis token shape"
```

---

### Task 3: Conditional tool functions and place / modify dispatch

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/conditional.py` (append tool functions)
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/tools.py` (`place_order`, `modify_order`)
- Modify: `pytossinvest-mcp/tests/conftest.py` (`FakeClient`, after `cancel_order`)
- Test: `pytossinvest-mcp/tests/test_conditional_tools.py`

**Interfaces:**
- Consumes: Task 1 functions; Task 2 `build_conditional_spec`, `CONDITIONAL_KIND`; `tools.AppContext`, `tools._price_and_currency(app, symbol) -> (last, currency)`; `paper.PaperError`; SDK client methods `create_conditional_order(*, symbol, type, quantity, order_type, expire_date, first, second=None, client_order_id=None, confirm_high_value_order=False) -> dict`, `modify_conditional_order(id, *, type, quantity, order_type, expire_date, first, second=None, confirm_high_value_order=False) -> dict`, `cancel_conditional_order(id) -> None`, `list_conditional_orders(status, symbol, cursor, limit) -> dict`, `get_conditional_order(id) -> dict`.
- Produces (all in `conditional.py`):
  - `preview_conditional_order(app, *, symbol, type, quantity, order_type, expire_date, first_side, first_trigger_price, first_order_price=None, second_side=None, second_trigger_price=None, second_order_price=None, confirm_high_value_order=False) -> dict` with keys `confirmationToken, clientOrderId, symbol, type, estimatedNotional, currency, expiresInSec, mode, next`.
  - `preview_conditional_modify(app, conditional_order_id, *, <same minus symbol>) -> dict` with keys `confirmationToken, conditionalOrderId, symbol, type, estimatedNotional, currency, expiresInSec, mode, next`.
  - `cancel_conditional_order(app, conditional_order_id) -> {"conditionalOrderId", "canceled": True}`.
  - `list_conditional_orders(app, status="OPEN", symbol=None, cursor=None) -> dict`, `get_conditional_order(app, conditional_order_id) -> dict`.
  - `execute_place(app, spec) -> dict`, `execute_modify(app, spec) -> dict`.

- [ ] **Step 1: Extend the fake client**

In `pytossinvest-mcp/tests/conftest.py`, inside `FakeClient`, after `cancel_order`:

```python

    # conditional orders (live path)
    def create_conditional_order(self, **kwargs):
        self.calls.append(("create_conditional_order", kwargs))
        return {"conditionalOrderId": "co-1", "clientOrderId": kwargs.get("client_order_id")}

    def modify_conditional_order(self, conditional_order_id, **kwargs):
        self.calls.append(("modify_conditional_order", conditional_order_id, kwargs))
        return {"conditionalOrderId": "co-2"}

    def cancel_conditional_order(self, conditional_order_id):
        self.calls.append(("cancel_conditional_order", conditional_order_id))

    def list_conditional_orders(self, status="OPEN", symbol=None, cursor=None, limit=20):
        self.calls.append(("list_conditional_orders", status, symbol, cursor))
        return {"conditionalOrders": [], "nextCursor": None, "hasNext": False}

    def get_conditional_order(self, conditional_order_id):
        self.calls.append(("get_conditional_order", conditional_order_id))
        return {"conditionalOrderId": conditional_order_id, "type": "SINGLE", "status": "WATCHING",
                "symbol": "005930", "market": "KR", "quantity": "10", "orderType": "LIMIT",
                "first": {"type": "STOP", "status": "WATCHING", "triggerPrice": "65000",
                          "orderPrice": "64900"}}
```

- [ ] **Step 2: Write the failing tests**

Create `pytossinvest-mcp/tests/test_conditional_tools.py`:

```python
import json
from datetime import date
from decimal import Decimal

import pytest

import pytossinvest_mcp.conditional as C
import pytossinvest_mcp.tools as T
from pytossinvest_mcp.paper import PaperError
from pytossinvest_mcp.safety import GuardrailError

DAY = date(2026, 6, 17).isoformat()  # conftest's SafetyManager "today"
SINGLE = dict(symbol="005930", type="SINGLE", quantity="10", order_type="LIMIT",
              expire_date="2026-07-31", first_side="SELL", first_trigger_price="65000",
              first_order_price="64900")                              # 649,000 KRW
MODIFY = {k: v for k, v in SINGLE.items() if k != "symbol"}


def _live(app_factory):
    return app_factory(mode="live", allow_live=True, enforce_market_hours=False)


def _spent(app):
    return app.safety.spend_store.current(DAY, "KRW")


def _audit(app, decision):
    with open(app.config.audit_log_path, encoding="utf-8") as f:
        return [e for e in map(json.loads, f) if e["decision"] == decision]


def test_place_registers_through_the_sdk_and_reserves_the_full_notional(app_factory, fake_client):
    app = _live(app_factory)
    pv = C.preview_conditional_order(app, **SINGLE)
    assert (pv["estimatedNotional"], pv["next"]) == ("649000", "place_order")

    out = T.place_order(app, confirmation_token=pv["confirmationToken"])

    assert out["conditionalOrderId"] == "co-1"
    sent = [c[1] for c in fake_client.calls if c[0] == "create_conditional_order"][-1]
    assert sent["client_order_id"] == pv["clientOrderId"]
    assert sent["first"] == {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "64900"}
    assert fake_client.place_payloads == []          # never the ordinary order endpoint
    assert _spent(app) == Decimal("649000")
    placed = _audit(app, "placed")[-1]
    assert (placed["kind"], placed["notional"]) == ("conditional", "649000")


def test_preview_enforces_guardrails_before_issuing_a_token(app_factory):
    app = _live(app_factory)
    oto = dict(SINGLE, type="OTO", first_side="BUY", first_trigger_price="60000",
               first_order_price="60100", second_side="SELL", second_trigger_price="70000",
               second_order_price="69900")
    with pytest.raises(GuardrailError) as e:   # 601,000 + 699,000 > 1,000,000 per order
        C.preview_conditional_order(app, **oto)
    assert e.value.code == "order-amount-cap"


def test_ambiguous_failure_keeps_the_reservation(app_factory, fake_client):
    from pytossinvest.errors import ServerError
    app = _live(app_factory)

    def down(**kwargs):
        raise ServerError("internal-error", "", http_status=503)
    fake_client.create_conditional_order = down

    pv = C.preview_conditional_order(app, **SINGLE)
    with pytest.raises(ServerError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("649000")


def test_rejection_releases_the_reservation(app_factory, fake_client):
    from pytossinvest.errors import BusinessRuleError
    app = _live(app_factory)

    def reject(**kwargs):
        raise BusinessRuleError("condition-already-met", "", http_status=422)
    fake_client.create_conditional_order = reject

    pv = C.preview_conditional_order(app, **SINGLE)
    with pytest.raises(BusinessRuleError):
        T.place_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("0")


def test_modify_reserves_only_the_delta(app_factory, fake_client):
    app = _live(app_factory)
    # original co-1: 10 x 64,900 = 649,000 ; new: 10 x 70,000 = 700,000 -> delta 51,000
    pv = C.preview_conditional_modify(app, "co-1", **dict(MODIFY, first_trigger_price="70100",
                                                          first_order_price="70000"))
    assert pv["next"] == "modify_order"

    out = T.modify_order(app, confirmation_token=pv["confirmationToken"])

    assert out["conditionalOrderId"] == "co-2"
    call = [c for c in fake_client.calls if c[0] == "modify_conditional_order"][-1]
    assert call[1] == "co-1" and call[2]["first"]["orderPrice"] == "70000"
    assert _spent(app) == Decimal("51000")
    assert _audit(app, "modified")[-1]["kind"] == "conditional"


def test_modify_counts_the_full_notional_when_the_original_cannot_be_priced(app_factory, fake_client):
    app = _live(app_factory)
    fake_client.get_conditional_order = lambda cid: {
        "conditionalOrderId": cid, "type": "SINGLE", "status": "WATCHING", "symbol": "005930",
        "quantity": "10", "orderType": "MARKET",
        "first": {"type": "PROFIT_RATE", "status": "WATCHING", "triggerPrice": None,
                  "targetProfitRate": "10"}}
    pv = C.preview_conditional_modify(app, "co-1", **MODIFY)
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert _spent(app) == Decimal("649000")


def test_cancel_is_audited_and_refunds_nothing(app_factory, fake_client):
    app = _live(app_factory)
    pv = C.preview_conditional_order(app, **SINGLE)
    T.place_order(app, confirmation_token=pv["confirmationToken"])

    out = C.cancel_conditional_order(app, "co-1")

    assert out == {"conditionalOrderId": "co-1", "canceled": True}
    assert ("cancel_conditional_order", "co-1") in fake_client.calls
    assert _spent(app) == Decimal("649000")
    assert _audit(app, "canceled")[-1]["previousStatus"] == "WATCHING"


def test_paper_writes_are_live_only_and_reads_are_empty(app_factory):
    app = app_factory(mode="paper")
    with pytest.raises(PaperError):
        C.preview_conditional_order(app, **SINGLE)
    with pytest.raises(PaperError):
        C.preview_conditional_modify(app, "co-1", **MODIFY)
    with pytest.raises(PaperError):
        C.cancel_conditional_order(app, "co-1")
    assert C.list_conditional_orders(app) == {"conditionalOrders": [], "hasNext": False}
    with pytest.raises(ValueError):
        C.get_conditional_order(app, "co-1")


def test_reads_use_the_real_account_outside_paper(app_factory, fake_client):
    app = app_factory(mode="read_only")
    C.list_conditional_orders(app, "CLOSED", cursor="c-2")
    assert ("list_conditional_orders", "CLOSED", None, "c-2") in fake_client.calls
    assert C.get_conditional_order(app, "co-1")["status"] == "WATCHING"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_conditional_tools.py`
Expected: FAIL — `AttributeError: module 'pytossinvest_mcp.conditional' has no attribute 'preview_conditional_order'`.

- [ ] **Step 4: Implement the tool functions**

Append to `conditional.py` (add `from .paper import PaperError` and `from .safety import CONDITIONAL_KIND` to the imports at the top; the `tools` import is local to avoid a cycle because `tools` imports this module lazily too):

```python


def _require_live(app) -> None:
    if app.use_paper:
        raise PaperError("paper mode has no conditional orders; they are live-only")


def _audit(app, tool: str, decision: str, **fields) -> None:
    app.audit.record({"tool": tool, "mode": app.config.mode, "decision": decision,
                      "kind": CONDITIONAL_KIND, **fields})


def preview_conditional_order(app, *, symbol: str, type: str, quantity: str, order_type: str,
                              expire_date: str, first_side: str, first_trigger_price: str,
                              first_order_price: str | None = None, second_side: str | None = None,
                              second_trigger_price: str | None = None,
                              second_order_price: str | None = None,
                              confirm_high_value_order: bool = False) -> dict:
    from .tools import _price_and_currency
    _require_live(app)
    request = build_request(
        type=type, quantity=quantity, order_type=order_type, expire_date=expire_date,
        first_side=first_side, first_trigger_price=first_trigger_price,
        first_order_price=first_order_price, second_side=second_side,
        second_trigger_price=second_trigger_price, second_order_price=second_order_price,
    )
    _, currency = _price_and_currency(app, symbol)
    spec = app.safety.build_conditional_spec(
        symbol=symbol, request=request, notional=request_notional(request),
        confirm_high_value_order=confirm_high_value_order, currency=currency,
    )
    # registering is allowed any time; the broker only fires during sessions
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False)
    token = app.safety.issue_token(spec)
    _audit(app, "preview_conditional_order", "previewed", symbol=symbol, notional=spec.notional,
           currency=spec.currency, clientOrderId=spec.client_order_id, token=token)
    return {"confirmationToken": token, "clientOrderId": spec.client_order_id, "symbol": symbol,
            "type": type, "estimatedNotional": str(spec.notional), "currency": spec.currency,
            "expiresInSec": app.config.confirmation_ttl_sec, "mode": app.config.mode,
            "next": "place_order"}


def preview_conditional_modify(app, conditional_order_id: str, *, type: str, quantity: str,
                               order_type: str, expire_date: str, first_side: str,
                               first_trigger_price: str, first_order_price: str | None = None,
                               second_side: str | None = None,
                               second_trigger_price: str | None = None,
                               second_order_price: str | None = None,
                               confirm_high_value_order: bool = False) -> dict:
    from .tools import _price_and_currency
    _require_live(app)
    request = build_request(
        type=type, quantity=quantity, order_type=order_type, expire_date=expire_date,
        first_side=first_side, first_trigger_price=first_trigger_price,
        first_order_price=first_order_price, second_side=second_side,
        second_trigger_price=second_trigger_price, second_order_price=second_order_price,
    )
    original = app.client.get_conditional_order(conditional_order_id)
    symbol = original["symbol"]
    _, currency = _price_and_currency(app, symbol)
    spec = app.safety.build_conditional_spec(
        symbol=symbol, request=request, notional=request_notional(request),
        confirm_high_value_order=confirm_high_value_order, currency=currency,
        modify_id=conditional_order_id,
    )
    spec.prev_notional = detail_notional(original)  # None -> the full new notional is counted
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                                check_daily=True, prev_notional=spec.prev_notional)
    token = app.safety.issue_token(spec)
    _audit(app, "preview_conditional_modify", "modify_previewed",
           conditionalOrderId=conditional_order_id, previousStatus=original.get("status"),
           symbol=symbol, notional=spec.notional, currency=spec.currency,
           clientOrderId=spec.client_order_id, token=token)
    return {"confirmationToken": token, "conditionalOrderId": conditional_order_id,
            "symbol": symbol, "type": type, "estimatedNotional": str(spec.notional),
            "currency": spec.currency, "expiresInSec": app.config.confirmation_ttl_sec,
            "mode": app.config.mode, "next": "modify_order"}


def cancel_conditional_order(app, conditional_order_id: str) -> dict:
    """Cancelling lowers risk, so no preview; it never refunds the registration-day cap."""
    _require_live(app)
    previous = app.client.get_conditional_order(conditional_order_id)
    app.client.cancel_conditional_order(conditional_order_id)
    _audit(app, "cancel_conditional_order", "canceled", conditionalOrderId=conditional_order_id,
           previousStatus=previous.get("status"))
    return {"conditionalOrderId": conditional_order_id, "canceled": True}


def list_conditional_orders(app, status: str = "OPEN", symbol: str | None = None,
                            cursor: str | None = None) -> dict:
    if app.use_paper:
        return {"conditionalOrders": [], "hasNext": False}
    return app.client.list_conditional_orders(status=status, symbol=symbol, cursor=cursor)


def get_conditional_order(app, conditional_order_id: str) -> dict:
    if app.use_paper:
        raise ValueError(f"paper conditional order not found: {conditional_order_id}")
    return app.client.get_conditional_order(conditional_order_id)


def execute_place(app, spec) -> dict:
    """The place_order execution step for a conditional token."""
    return app.client.create_conditional_order(
        symbol=spec.symbol, client_order_id=spec.client_order_id,
        confirm_high_value_order=spec.confirm_high_value_order, **spec.conditional,
    )


def execute_modify(app, spec) -> dict:
    """The modify_order execution step for a conditional modify token (returns a new id)."""
    return app.client.modify_conditional_order(
        spec.modify_order_id, confirm_high_value_order=spec.confirm_high_value_order,
        **spec.conditional,
    )
```

- [ ] **Step 5: Implement the dispatch in `tools.py`**

In the imports at the top of `tools.py`, change `from .safety import GuardrailError, SafetyManager, order_currency` to:

```python
from .safety import CONDITIONAL_KIND, GuardrailError, SafetyManager, order_currency
```

In `place_order`, replace the first line inside `try:` (`        if app.use_paper:`) with:

```python
        if spec.kind == CONDITIONAL_KIND:
            from .conditional import execute_place
            result = execute_place(app, spec)
        elif app.use_paper:
```

and in the success audit of `place_order` add `"kind": spec.kind,` after `"decision": "placed",`.

In `modify_order`, replace the body of `try:`:

```python
        result = app.client.modify_order(
            spec.modify_order_id, order_type=spec.order_type,
            price=spec.price, quantity=spec.quantity,
            confirm_high_value_order=spec.confirm_high_value_order,
        )
```

with:

```python
        if spec.kind == CONDITIONAL_KIND:
            from .conditional import execute_modify
            result = execute_modify(app, spec)
        else:
            result = app.client.modify_order(
                spec.modify_order_id, order_type=spec.order_type,
                price=spec.price, quantity=spec.quantity,
                confirm_high_value_order=spec.confirm_high_value_order,
            )
```

and in the success audit of `modify_order` add `"kind": spec.kind,` after `"decision": "modified",`.

- [ ] **Step 6: Run to verify pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`
Expected: all pass (new file plus every existing test).

- [ ] **Step 7: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/conditional.py pytossinvest-mcp/src/pytossinvest_mcp/tools.py pytossinvest-mcp/tests/conftest.py pytossinvest-mcp/tests/test_conditional_tools.py
git commit -m "feat(mcp): conditional order tools, placed and modified through the token path"
```

---

### Task 4: Register the tools

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/server.py` (`Literal` aliases near `Side = ...`; `_register_reads` after `get_order`; `_register_writes` after `cancel_order`)
- Test: `pytossinvest-mcp/tests/test_server_modes.py` (the `READ_TOOLS` / `WRITE_TOOLS` sets at the top; append tests)

**Interfaces:**
- Consumes: Task 3 functions in `conditional.py`.
- Produces: MCP tools `list_conditional_orders`, `get_conditional_order` (all modes) and `preview_conditional_order`, `preview_conditional_modify`, `cancel_conditional_order` (paper / live).

- [ ] **Step 1: Write the failing tests**

In `test_server_modes.py`, change the two sets at the top to:

```python
READ_TOOLS = {"get_accounts", "get_holdings", "get_buying_power", "get_quote", "get_candles",
              "get_stock_info", "get_market_info", "list_orders", "get_order",
              "list_conditional_orders", "get_conditional_order"}
WRITE_TOOLS = {"get_order_readiness", "preview_order", "place_order",
               "preview_modify", "modify_order", "cancel_order",
               "preview_conditional_order", "preview_conditional_modify",
               "cancel_conditional_order"}
```

and append:

```python


def test_conditional_params_advertise_enums(tmp_path):
    mcp = _build(tmp_path, "live", allow_live=True)
    preview = _props(mcp, "preview_conditional_order")
    assert preview["type"]["enum"] == ["SINGLE", "OCO", "OTO"]
    assert preview["order_type"]["enum"] == ["LIMIT", "MARKET"]
    assert preview["first_side"]["enum"] == ["BUY", "SELL"]
    assert "symbol" not in _props(mcp, "preview_conditional_modify")
    assert _props(mcp, "list_conditional_orders")["status"]["enum"] == ["OPEN", "CLOSED"]


def test_conditional_validation_reaches_the_model(tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    mcp = _build(tmp_path, "live", allow_live=True)
    args = {"symbol": "005930", "type": "OCO", "quantity": "10", "order_type": "LIMIT",
            "expire_date": "2026-07-31", "first_side": "SELL", "first_trigger_price": "80000",
            "first_order_price": "79900"}
    with pytest.raises(ToolError) as e:
        asyncio.run(mcp.call_tool("preview_conditional_order", args))
    assert not isinstance(e.value, UnexpectedToolError)
    assert "second_side" in str(e.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_server_modes.py`
Expected: FAIL — registered tool sets differ; `KeyError: 'preview_conditional_order'`.

- [ ] **Step 3: Implement**

In `server.py`, add `from . import conditional as CO` next to `from . import tools as T`, and next to the other aliases:

```python
ConditionalType = Literal["SINGLE", "OCO", "OTO"]
```

At the end of `_register_reads` (after `get_order`):

```python

    @mcp_tool(name="list_conditional_orders",
              description="Conditional (trigger-price) orders: OPEN (watching/ordering) or CLOSED "
                          "(completed/expired), paged with nextCursor -> cursor. Includes ones "
                          "made in other channels such as the app. Paper returns none.")
    def list_conditional_orders(status: OrderStatus = "OPEN", symbol: "str | None" = None,
                                cursor: "str | None" = None) -> dict:
        return CO.list_conditional_orders(app, status, symbol, cursor)

    @mcp_tool(name="get_conditional_order",
              description="One conditional order by id, with each condition's status and the id "
                          "of the order it fired, if any.")
    def get_conditional_order(conditional_order_id: str) -> dict:
        return CO.get_conditional_order(app, conditional_order_id)
```

At the end of `_register_writes` (after `cancel_order`):

```python

    @mcp_tool(name="preview_conditional_order",
              description="STEP 1 of 2 for a conditional order the broker fires when a trigger "
                          "price is reached, until expire_date (YYYY-MM-DD). SINGLE = one condition; "
                          "OCO = two SELL conditions, first fires or second (take-profit above, "
                          "stop below the current price); OTO = first BUY, then second SELL after "
                          "it fills. LIMIT needs an order price per condition, MARKET takes none. "
                          "Validates guardrails and counts the full amount against today's cap; "
                          "then call place_order with the confirmation_token. live only. "
                          "Money/quantity are strings.")
    def preview_conditional_order(symbol: str, type: ConditionalType, quantity: str,
                                  order_type: OrderType, expire_date: str, first_side: Side,
                                  first_trigger_price: str, first_order_price: "str | None" = None,
                                  second_side: Side | None = None,
                                  second_trigger_price: "str | None" = None,
                                  second_order_price: "str | None" = None,
                                  confirm_high_value_order: bool = False) -> dict:
        return CO.preview_conditional_order(
            app, symbol=symbol, type=type, quantity=quantity, order_type=order_type,
            expire_date=expire_date, first_side=first_side,
            first_trigger_price=first_trigger_price, first_order_price=first_order_price,
            second_side=second_side, second_trigger_price=second_trigger_price,
            second_order_price=second_order_price,
            confirm_high_value_order=confirm_high_value_order,
        )

    @mcp_tool(name="preview_conditional_modify",
              description="STEP 1 of 2 to replace a conditional order entirely (same fields as "
                          "preview_conditional_order, no symbol). The broker cancels and re-creates "
                          "it, so the result has a NEW id. Counts only the change in amount against "
                          "today's cap; then call modify_order with the confirmation_token. live only.")
    def preview_conditional_modify(conditional_order_id: str, type: ConditionalType, quantity: str,
                                   order_type: OrderType, expire_date: str, first_side: Side,
                                   first_trigger_price: str, first_order_price: "str | None" = None,
                                   second_side: Side | None = None,
                                   second_trigger_price: "str | None" = None,
                                   second_order_price: "str | None" = None,
                                   confirm_high_value_order: bool = False) -> dict:
        return CO.preview_conditional_modify(
            app, conditional_order_id, type=type, quantity=quantity, order_type=order_type,
            expire_date=expire_date, first_side=first_side,
            first_trigger_price=first_trigger_price, first_order_price=first_order_price,
            second_side=second_side, second_trigger_price=second_trigger_price,
            second_order_price=second_order_price,
            confirm_high_value_order=confirm_high_value_order,
        )

    @mcp_tool(name="cancel_conditional_order",
              description="Cancel a conditional order that has not fired. live only. Does not "
                          "refund today's cap.")
    def cancel_conditional_order(conditional_order_id: str) -> dict:
        return CO.cancel_conditional_order(app, conditional_order_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/server.py pytossinvest-mcp/tests/test_server_modes.py
git commit -m "feat(mcp): register the conditional order tools"
```

---

### Task 5: Docs

**Files:**
- Modify: `AGENTS.md` (tool count 15 → 20 in Tech Stack line and wiki index line; MCP test count; the `place_order` invariant paragraph gets one sentence that conditional tokens take the same path)
- Modify: `docs/wiki/pytossinvest-mcp.md` (tool section heading and list; a "조건주문" subsection with modes table, notional rule, daily-cap rule, idempotency limit; test count)
- Modify: `pytossinvest-mcp/README.md` (tool count heading; rows for the five tools)
- Modify: `README.md` (tool count and the read / write lists)
- Modify: `docs/wiki/tossinvest-open-api.md` (implementation-status note: MCP now exposes conditional orders; streams still not)

- [ ] **Step 1: Update the documents** with the facts from the spec (modes table, SINGLE / OCO / OTO notional, registration-day accounting, no refund on cancel, modify has no idempotency key, market-hours gate skipped, paper live-only). Use the exact tool names and parameter names from Task 4.

- [ ] **Step 2: Verify**

Run: `grep -n "15 툴\|15툴\|15개\|(201)" AGENTS.md README.md pytossinvest-mcp/README.md docs/wiki/pytossinvest-mcp.md`
Expected: no stale counts. Then run both suites; all pass.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md README.md pytossinvest-mcp/README.md docs/wiki/pytossinvest-mcp.md docs/wiki/tossinvest-open-api.md
git commit -m "docs: conditional orders over MCP"
```

---

### Task 6: Ship and verify

- [ ] **Step 1:** `git switch main && git merge --no-ff feat/conditional-orders-mcp -m "Merge branch 'feat/conditional-orders-mcp': conditional orders over MCP"`, run both suites, `git push origin main`, wait for CI (`gh run list --limit 1`) to report success.
- [ ] **Step 2:** OCI: `ssh zerry-cf` → `cd ~/infra/tossinvest-mcp/tossinvest && git pull --ff-only origin main` → `cd .. && docker compose build && docker compose up -d`.
- [ ] **Step 3:** Live check inside the container with `/app/.venv/bin/python` (not `uv run`): `tools/list` shows 11 read tools in `read_only`, `list_conditional_orders` (OPEN and CLOSED) returns 200 with `conditionalOrders` / `hasNext`, and `get_conditional_order` on an id from the list (if any) returns its detail. Print counts / statuses only. Do not exercise write tools live.
- [ ] **Step 4:** Rebuild the local image: `docker compose -f deploy/docker-compose.yml build mcp` (do not start containers).
