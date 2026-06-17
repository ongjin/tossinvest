# pytossinvest SDK Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `pytossinvest`, a Python client SDK for the Toss Securities Open API that correctly handles the API's traps — string-decimal money, per-group rate limits, OAuth token lifecycle, idempotency, and `code`-based error mapping — fully testable without live credentials.

**Architecture:** A `uv` workspace monorepo. This plan builds the `pytossinvest` member only (the MCP server is Plan 2). The SDK is layered: pure helpers (`money`, `errors`, `ratelimit`) → I/O units (`auth`) → typed `models` → `client` that wires everything and exposes endpoint methods. Every unit is unit-tested with `respx` mocking httpx; no real network.

**Tech Stack:** Python 3.12, `uv` (workspace + deps), `httpx` (sync), `pydantic` v2 (decimal-safe models), `pytest` + `respx` (tests). License: MIT.

**Design decisions locked from the spec (`docs/superpowers/specs/2026-06-17-tossinvest-mcp-design.md`):**
- Money/quantity are **strings → `Decimal`**, never `float`. A dedicated `money` module is the only conversion path.
- Errors branch on the flat `code` string; **unknown codes/HTTP statuses must not crash** (fall back to base `TossInvestError`/`ServerError`).
- The token endpoint (`POST /oauth2/token`) uses the **OAuth2 format** (no `result` wrapping; `{error, error_description}` on failure). Every other endpoint wraps payload in `result` and errors in `ErrorResponse`.
- Rate limiting is **per API group**; response headers (`X-RateLimit-*`, `Retry-After`) are the source of truth, not hardcoded numbers. `ORDER`/`ORDER_INFO` halve during 09:00–09:10 KST.
- `accountSeq` is fetched once and cached (`ACCOUNT` group is 1/s).
- v1 **fully types** the core endpoints (accounts, holdings, prices, buying-power, orders); thinner endpoints return the unwrapped `result` as plain Python. This is a deliberate progressive-typing choice, not a TODO.

---

## File Structure

```
toss/
  pyproject.toml                       # uv workspace root: members = ["pytossinvest"]
  pytossinvest/
    pyproject.toml                     # package metadata, deps, MIT license field
    LICENSE                            # MIT
    README.md
    src/pytossinvest/
      __init__.py                      # public exports
      money.py                         # to_decimal / decimal_to_str
      errors.py                        # exception hierarchy + error_from_response / oauth_error_from_response
      ratelimit.py                     # TokenBucket + effective_rate (peak-hour) + RateLimiter
      auth.py                          # TokenManager (fetch/cache/refresh/invalidate)
      models.py                        # pydantic response models (decimal-safe)
      client.py                        # TossInvestClient (request plumbing + endpoint methods)
    tests/
      test_money.py
      test_errors.py
      test_ratelimit.py
      test_auth.py
      test_models.py
      test_client_core.py              # _request plumbing: unwrap, error mapping, account header, token retry, ratelimit hook
      test_client_endpoints.py         # accounts/holdings/prices/buying-power/orders
```

Responsibilities: `money` (pure), `errors` (pure), `ratelimit` (pure + injected clock), `auth` (httpx I/O), `models` (schema), `client` (orchestration). Each file has one job and is held in context independently.

---

## Task 1: Workspace scaffold + first green test

**Files:**
- Create: `pyproject.toml` (workspace root)
- Create: `pytossinvest/pyproject.toml`
- Create: `pytossinvest/LICENSE`
- Create: `pytossinvest/src/pytossinvest/__init__.py`
- Create: `pytossinvest/tests/test_smoke.py`

- [ ] **Step 1: Create workspace root `pyproject.toml`**

```toml
[tool.uv.workspace]
members = ["pytossinvest"]
```

- [ ] **Step 2: Create `pytossinvest/pyproject.toml`**

```toml
[project]
name = "pytossinvest"
version = "0.0.1"
description = "Unofficial Python client for the Toss Securities Open API"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
dependencies = ["httpx>=0.27", "pydantic>=2.7"]

[project.optional-dependencies]
dev = ["pytest>=8", "respx>=0.21"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/pytossinvest"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 3: Create `pytossinvest/LICENSE`** — paste the standard MIT License text, copyright line: `Copyright (c) 2026 ongjin`.

- [ ] **Step 4: Create `pytossinvest/README.md`** with a one-paragraph description ("Unofficial Python client for the Toss Securities Open API. MIT licensed.") and a `## Status` note that the API is in pre-launch and the SDK is tested against fixtures.

- [ ] **Step 5: Create `pytossinvest/src/pytossinvest/__init__.py`**

```python
__version__ = "0.0.1"
```

- [ ] **Step 6: Write the smoke test `pytossinvest/tests/test_smoke.py`**

```python
import pytossinvest


def test_version_exposed():
    assert pytossinvest.__version__ == "0.0.1"
```

- [ ] **Step 7: Sync and run**

Run: `cd /Users/cyj/workspace/personal/toss && uv sync --package pytossinvest --extra dev`
Then: `uv run --package pytossinvest pytest pytossinvest/tests/test_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 8: Commit**

```bash
cd /Users/cyj/workspace/personal/toss
git add pyproject.toml pytossinvest/
git commit -m "chore: scaffold pytossinvest package (uv workspace, MIT)"
```

---

## Task 2: `money.py` — decimal-safe conversion

**Files:**
- Create: `pytossinvest/src/pytossinvest/money.py`
- Test: `pytossinvest/tests/test_money.py`

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal

import pytest

from pytossinvest.money import to_decimal, decimal_to_str


def test_to_decimal_from_string():
    assert to_decimal("70000") == Decimal("70000")
    assert to_decimal("0.1516") == Decimal("0.1516")


def test_to_decimal_from_int_and_decimal():
    assert to_decimal(10) == Decimal("10")
    assert to_decimal(Decimal("5")) == Decimal("5")


def test_to_decimal_rejects_float():
    with pytest.raises(TypeError):
        to_decimal(0.1)


def test_to_decimal_rejects_bool():
    with pytest.raises(TypeError):
        to_decimal(True)


def test_decimal_to_str_roundtrip():
    assert decimal_to_str(to_decimal("70000")) == "70000"
    assert decimal_to_str(to_decimal("0.10")) == "0.10"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_money.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pytossinvest.money'`.

- [ ] **Step 3: Implement `money.py`**

```python
from decimal import Decimal

__all__ = ["to_decimal", "decimal_to_str"]


def to_decimal(value: "str | int | Decimal") -> Decimal:
    """Convert an API money/quantity value to Decimal. Floats are forbidden."""
    if isinstance(value, bool):
        raise TypeError("bool is not a valid money/quantity value")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(
        f"refusing to convert {type(value).__name__} to Decimal (float forbidden)"
    )


def decimal_to_str(value: Decimal) -> str:
    """Serialize a Decimal to the plain string form the API expects."""
    return format(value, "f")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_money.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest/src/pytossinvest/money.py pytossinvest/tests/test_money.py
git commit -m "feat(sdk): decimal-safe money conversion (float forbidden)"
```

---

## Task 3: `errors.py` — exception hierarchy + response mapping

**Files:**
- Create: `pytossinvest/src/pytossinvest/errors.py`
- Test: `pytossinvest/tests/test_errors.py`

- [ ] **Step 1: Write the failing test**

```python
from pytossinvest.errors import (
    TossInvestError,
    AuthError,
    NotFoundError,
    ValidationError,
    ConflictError,
    BusinessRuleError,
    RateLimitError,
    ServerError,
    OAuthError,
    error_from_response,
    oauth_error_from_response,
)


def _body(code, message="", data=None, request_id="01HXY"):
    err = {"code": code, "message": message, "requestId": request_id}
    if data is not None:
        err["data"] = data
    return {"error": err}


def test_maps_status_to_class():
    assert isinstance(error_from_response(400, _body("invalid-request")), ValidationError)
    assert isinstance(error_from_response(401, _body("expired-token")), AuthError)
    assert isinstance(error_from_response(404, _body("order-not-found")), NotFoundError)
    assert isinstance(error_from_response(409, _body("already-filled")), ConflictError)
    assert isinstance(error_from_response(422, _body("insufficient-buying-power")), BusinessRuleError)
    assert isinstance(error_from_response(500, _body("internal-error")), ServerError)


def test_preserves_code_and_metadata():
    err = error_from_response(
        422, _body("price-out-of-range", "bad", data={"field": "price"})
    )
    assert err.code == "price-out-of-range"
    assert err.request_id == "01HXY"
    assert err.data == {"field": "price"}
    assert err.http_status == 422


def test_rate_limit_reads_retry_after():
    err = error_from_response(429, _body("rate-limit-exceeded"), headers={"Retry-After": "3"})
    assert isinstance(err, RateLimitError)
    assert err.retry_after == 3.0


def test_unknown_code_does_not_crash():
    err = error_from_response(400, _body("brand-new-code-from-server"))
    assert isinstance(err, ValidationError)
    assert err.code == "brand-new-code-from-server"


def test_unknown_status_falls_back_to_base():
    err = error_from_response(418, _body("teapot"))
    assert type(err) is TossInvestError
    assert err.code == "teapot"


def test_empty_message_is_tolerated():
    err = error_from_response(401, {"error": {"code": "invalid-token"}})
    assert err.code == "invalid-token"
    assert err.message == ""


def test_oauth_error_separate_format():
    err = oauth_error_from_response(401, {"error": "invalid_client", "error_description": "bad secret"})
    assert isinstance(err, OAuthError)
    assert err.code == "invalid_client"
    assert err.message == "bad secret"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pytossinvest.errors'`.

- [ ] **Step 3: Implement `errors.py`**

```python
from __future__ import annotations

__all__ = [
    "TossInvestError",
    "AuthError",
    "ForbiddenError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "BusinessRuleError",
    "RateLimitError",
    "ServerError",
    "OAuthError",
    "error_from_response",
    "oauth_error_from_response",
]


class TossInvestError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        http_status: int | None = None,
        request_id: str | None = None,
        data: dict | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(f"[{http_status}] {code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.request_id = request_id
        self.data = data or {}
        self.retry_after = retry_after


class AuthError(TossInvestError): ...
class ForbiddenError(TossInvestError): ...
class NotFoundError(TossInvestError): ...
class ValidationError(TossInvestError): ...
class ConflictError(TossInvestError): ...
class BusinessRuleError(TossInvestError): ...
class RateLimitError(TossInvestError): ...
class ServerError(TossInvestError): ...
class OAuthError(TossInvestError): ...


_STATUS_MAP: dict[int, type[TossInvestError]] = {
    400: ValidationError,
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    422: BusinessRuleError,
    429: RateLimitError,
}


def error_from_response(
    http_status: int, body: dict | None, headers: dict | None = None
) -> TossInvestError:
    headers = headers or {}
    err = (body or {}).get("error") or {}
    code = err.get("code", "unknown")
    message = err.get("message", "")
    request_id = err.get("requestId")
    data = err.get("data")

    retry_after = None
    if http_status == 429:
        raw = headers.get("Retry-After")
        if raw is not None:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = None

    cls = _STATUS_MAP.get(http_status)
    if cls is None:
        cls = ServerError if http_status >= 500 else TossInvestError

    return cls(
        code,
        message,
        http_status=http_status,
        request_id=request_id,
        data=data,
        retry_after=retry_after,
    )


def oauth_error_from_response(http_status: int, body: dict | None) -> OAuthError:
    body = body or {}
    return OAuthError(
        body.get("error", "unknown"),
        body.get("error_description", ""),
        http_status=http_status,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_errors.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest/src/pytossinvest/errors.py pytossinvest/tests/test_errors.py
git commit -m "feat(sdk): error hierarchy + code-based mapping (unknown-tolerant)"
```

---

## Task 4: `ratelimit.py` — token bucket + peak-hour rate

**Files:**
- Create: `pytossinvest/src/pytossinvest/ratelimit.py`
- Test: `pytossinvest/tests/test_ratelimit.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime

from pytossinvest.ratelimit import TokenBucket, effective_rate


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_bucket_starts_full():
    clock = FakeClock()
    b = TokenBucket(capacity=5, refill_per_sec=5, now=clock)
    assert all(b.try_acquire() for _ in range(5))
    assert b.try_acquire() is False


def test_bucket_refills_over_time():
    clock = FakeClock()
    b = TokenBucket(capacity=5, refill_per_sec=5, now=clock)
    for _ in range(5):
        b.try_acquire()
    assert b.try_acquire() is False
    clock.advance(0.2)  # 0.2s * 5/s = 1 token
    assert b.try_acquire() is True


def test_time_until_available():
    clock = FakeClock()
    b = TokenBucket(capacity=1, refill_per_sec=2, now=clock)
    assert b.try_acquire() is True
    # need 1 token at 2/s -> 0.5s
    assert b.time_until_available() == 0.5


def test_capacity_never_exceeded():
    clock = FakeClock()
    b = TokenBucket(capacity=3, refill_per_sec=10, now=clock)
    clock.advance(100)
    granted = sum(1 for _ in range(10) if b.try_acquire())
    assert granted == 3


def test_peak_hour_halves_order_groups():
    peak = datetime(2026, 6, 17, 9, 5)
    off = datetime(2026, 6, 17, 10, 0)
    assert effective_rate("ORDER", 6, peak) == 3
    assert effective_rate("ORDER_INFO", 6, peak) == 3
    assert effective_rate("ORDER", 6, off) == 6
    assert effective_rate("MARKET_DATA", 10, peak) == 10  # not an order group
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_ratelimit.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `ratelimit.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable

__all__ = ["TokenBucket", "effective_rate", "PEAK_GROUPS"]

PEAK_GROUPS = {"ORDER", "ORDER_INFO"}
_PEAK_START = time(9, 0)
_PEAK_END = time(9, 10)


@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    now: Callable[[], float]
    _tokens: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last = self.now()

    def _refill(self) -> None:
        t = self.now()
        elapsed = t - self._last
        self._last = t
        self._tokens = min(
            self.capacity, self._tokens + elapsed * self.refill_per_sec
        )

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def time_until_available(self, n: float = 1.0) -> float:
        self._refill()
        if self._tokens >= n:
            return 0.0
        return (n - self._tokens) / self.refill_per_sec


def effective_rate(group: str, base_rate: float, now_kst: datetime) -> float:
    """Order groups are halved during the 09:00-09:10 KST open auction window."""
    if group in PEAK_GROUPS and _PEAK_START <= now_kst.time() < _PEAK_END:
        return base_rate / 2
    return base_rate
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_ratelimit.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest/src/pytossinvest/ratelimit.py pytossinvest/tests/test_ratelimit.py
git commit -m "feat(sdk): token-bucket rate limiter + peak-hour halving"
```

---

## Task 5: `auth.py` — token manager

**Files:**
- Create: `pytossinvest/src/pytossinvest/auth.py`
- Test: `pytossinvest/tests/test_auth.py`

The `TokenManager` fetches an OAuth token, caches it until `expires_in` minus a safety buffer, and exposes `invalidate()` so the client can force a refresh after a `401 expired-token`. Time is injected (`now`) for deterministic tests.

- [ ] **Step 1: Write the failing test**

```python
import httpx
import respx

from pytossinvest.auth import TokenManager
from pytossinvest.errors import OAuthError
import pytest

BASE = "https://openapi.tossinvest.com"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _mgr(clock):
    http = httpx.Client(base_url=BASE)
    return TokenManager("cid", "secret", http=http, now=clock), http


@respx.mock
def test_fetches_and_caches_token():
    clock = FakeClock()
    route = respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600})
    )
    mgr, _ = _mgr(clock)
    assert mgr.get_token() == "tok-1"
    assert mgr.get_token() == "tok-1"  # cached
    assert route.call_count == 1


@respx.mock
def test_refreshes_after_expiry():
    clock = FakeClock()
    respx.post(f"{BASE}/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 100}),
            httpx.Response(200, json={"access_token": "tok-2", "token_type": "Bearer", "expires_in": 100}),
        ]
    )
    mgr, _ = _mgr(clock)
    assert mgr.get_token() == "tok-1"
    clock.advance(200)  # well past expiry
    assert mgr.get_token() == "tok-2"


@respx.mock
def test_invalidate_forces_refetch():
    clock = FakeClock()
    respx.post(f"{BASE}/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-2", "token_type": "Bearer", "expires_in": 3600}),
        ]
    )
    mgr, _ = _mgr(clock)
    assert mgr.get_token() == "tok-1"
    mgr.invalidate()
    assert mgr.get_token() == "tok-2"


@respx.mock
def test_oauth_failure_raises_oauth_error():
    clock = FakeClock()
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_client", "error_description": "bad"})
    )
    mgr, _ = _mgr(clock)
    with pytest.raises(OAuthError) as exc:
        mgr.get_token()
    assert exc.value.code == "invalid_client"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `auth.py`**

```python
from __future__ import annotations

import time as _time
from typing import Callable

import httpx

from .errors import oauth_error_from_response

__all__ = ["TokenManager"]

_EXPIRY_BUFFER_SEC = 30.0


class TokenManager:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.Client,
        now: Callable[[], float] = _time.monotonic,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._now = now
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        if self._token is not None and self._now() < self._expires_at:
            return self._token
        return self._fetch()

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def _fetch(self) -> str:
        resp = self._http.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = resp.json()
        if resp.status_code != 200:
            raise oauth_error_from_response(resp.status_code, body)
        self._token = body["access_token"]
        self._expires_at = self._now() + float(body["expires_in"]) - _EXPIRY_BUFFER_SEC
        return self._token
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_auth.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest/src/pytossinvest/auth.py pytossinvest/tests/test_auth.py
git commit -m "feat(sdk): OAuth token manager (cache/refresh/invalidate)"
```

---

## Task 6: `models.py` — decimal-safe response models

**Files:**
- Create: `pytossinvest/src/pytossinvest/models.py`
- Test: `pytossinvest/tests/test_models.py`

Only the core endpoints get typed models in v1 (accounts, holdings, prices, buying-power, orders). All money/quantity fields are `Decimal`; pydantic coerces from the API's string values and never produces a float.

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal

from pytossinvest.models import Account, Price, BuyingPower, OrderResponse, HoldingsItem


def test_account_parses():
    a = Account.model_validate({"accountNo": "123-45", "accountSeq": 1, "accountType": "BROKERAGE"})
    assert a.account_seq == 1
    assert a.account_type == "BROKERAGE"


def test_price_money_is_decimal():
    p = Price.model_validate({"symbol": "005930", "lastPrice": "70000", "currency": "KRW"})
    assert p.last_price == Decimal("70000")
    assert isinstance(p.last_price, Decimal)


def test_buying_power_decimal():
    bp = BuyingPower.model_validate({"currency": "KRW", "cashBuyingPower": "1000000"})
    assert bp.cash_buying_power == Decimal("1000000")


def test_order_response():
    o = OrderResponse.model_validate({"orderId": "ord-1", "clientOrderId": "cli-1"})
    assert o.order_id == "ord-1"
    assert o.client_order_id == "cli-1"


def test_holdings_item_decimal_quantity():
    item = HoldingsItem.model_validate(
        {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "currency": "KRW",
         "quantity": "10", "lastPrice": "70000", "averagePurchasePrice": "65000"}
    )
    assert item.quantity == Decimal("10")
    assert item.average_purchase_price == Decimal("65000")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `models.py`**

```python
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Account", "Price", "BuyingPower", "OrderResponse", "HoldingsItem"]


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Account(_Base):
    account_no: str = Field(alias="accountNo")
    account_seq: int = Field(alias="accountSeq")
    account_type: str = Field(alias="accountType")


class Price(_Base):
    symbol: str
    last_price: Decimal = Field(alias="lastPrice")
    currency: str
    timestamp: str | None = None


class BuyingPower(_Base):
    currency: str
    cash_buying_power: Decimal = Field(alias="cashBuyingPower")


class OrderResponse(_Base):
    order_id: str = Field(alias="orderId")
    client_order_id: str | None = Field(default=None, alias="clientOrderId")


class HoldingsItem(_Base):
    symbol: str
    name: str
    market_country: str = Field(alias="marketCountry")
    currency: str
    quantity: Decimal
    last_price: Decimal = Field(alias="lastPrice")
    average_purchase_price: Decimal = Field(alias="averagePurchasePrice")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_models.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest/src/pytossinvest/models.py pytossinvest/tests/test_models.py
git commit -m "feat(sdk): decimal-safe pydantic models for core endpoints"
```

---

## Task 7: `client.py` — request plumbing (core)

**Files:**
- Create: `pytossinvest/src/pytossinvest/client.py`
- Test: `pytossinvest/tests/test_client_core.py`

This task builds `TossInvestClient` and its private `_request` method, which orchestrates: rate-limit gate (per group), `Authorization` header from `TokenManager`, optional `X-Tossinvest-Account` header, `result` unwrapping, error mapping, and one automatic retry after `401 expired-token`. Endpoint methods come in Task 8.

`_request` contract:
- Signature: `_request(self, method, path, *, group, account=False, params=None, json=None, data=None) -> Any`
- Gates on `self._buckets[group]` before sending (sleep via injected `sleep` until a token is available).
- On `401` with `code == "expired-token"`, calls `token.invalidate()` and retries **once**.
- On `429`, raises `RateLimitError` (the bucket already paces; retry/backoff orchestration lives one layer up in Task 9's wiring — for v1 the bucket + raised error is the contract).
- Non-2xx → `error_from_response(...)`. 2xx → returns `body["result"]`.

- [ ] **Step 1: Write the failing test**

```python
import httpx
import respx
import pytest

from pytossinvest.client import TossInvestClient
from pytossinvest.errors import ValidationError, RateLimitError

BASE = "https://openapi.tossinvest.com"


def _token_route():
    return respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
    )


def _client():
    # sleep is a no-op so rate-limit waits don't slow tests
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)


@respx.mock
def test_unwraps_result_and_sends_auth():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]})
    )
    c = _client()
    result = c._request("GET", "/api/v1/accounts", group="ACCOUNT")
    assert result == [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_account_header_added_when_requested():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json={"result": {"items": []}})
    )
    c = _client()
    c._account_seq = 3  # pretend cached
    c._request("GET", "/api/v1/holdings", group="ASSET", account=True)
    assert route.calls.last.request.headers["X-Tossinvest-Account"] == "3"


@respx.mock
def test_maps_error_by_status():
    _token_route()
    respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(400, json={"error": {"code": "account-header-required", "message": ""}})
    )
    c = _client()
    with pytest.raises(ValidationError) as exc:
        c._request("GET", "/api/v1/holdings", group="ASSET")
    assert exc.value.code == "account-header-required"


@respx.mock
def test_retries_once_on_expired_token():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"code": "expired-token", "message": ""}}),
            httpx.Response(200, json={"result": []}),
        ]
    )
    c = _client()
    result = c._request("GET", "/api/v1/accounts", group="ACCOUNT")
    assert result == []
    assert route.call_count == 2


@respx.mock
def test_429_raises_rate_limit_error():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "2"})
    )
    c = _client()
    with pytest.raises(RateLimitError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert exc.value.retry_after == 2.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_client_core.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `client.py` (core only)**

```python
from __future__ import annotations

import time as _time
from typing import Any, Callable

import httpx

from .auth import TokenManager
from .errors import error_from_response
from .ratelimit import TokenBucket

__all__ = ["TossInvestClient"]

# Base TPS per group (header values override at runtime; these are the documented defaults).
_GROUP_RATES: dict[str, float] = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "ORDER": 6,
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,
}


class TossInvestClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = "https://openapi.tossinvest.com",
        timeout: float = 10.0,
        sleep: Callable[[float], None] = _time.sleep,
        monotonic: Callable[[], float] = _time.monotonic,
    ):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._token = TokenManager(client_id, client_secret, http=self._http, now=monotonic)
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {
            g: TokenBucket(capacity=r, refill_per_sec=r, now=monotonic)
            for g, r in _GROUP_RATES.items()
        }
        self._account_seq: int | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TossInvestClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _gate(self, group: str) -> None:
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        while not bucket.try_acquire():
            self._sleep(bucket.time_until_available())

    def _request(
        self,
        method: str,
        path: str,
        *,
        group: str,
        account: bool = False,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
        _retried: bool = False,
    ) -> Any:
        self._gate(group)
        headers = {"Authorization": f"Bearer {self._token.get_token()}"}
        if account:
            if self._account_seq is None:
                raise RuntimeError("account context required but accountSeq not cached; call get_accounts() first")
            headers["X-Tossinvest-Account"] = str(self._account_seq)

        resp = self._http.request(
            method, path, params=params, json=json, data=data, headers=headers
        )

        if resp.status_code == 200:
            return resp.json().get("result")

        body = resp.json()
        if (
            resp.status_code == 401
            and not _retried
            and (body.get("error") or {}).get("code") == "expired-token"
        ):
            self._token.invalidate()
            return self._request(
                method, path, group=group, account=account,
                params=params, json=json, data=data, _retried=True,
            )

        raise error_from_response(resp.status_code, body, dict(resp.headers))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_client_core.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pytossinvest/src/pytossinvest/client.py pytossinvest/tests/test_client_core.py
git commit -m "feat(sdk): client request plumbing (auth, account header, unwrap, retry, ratelimit)"
```

---

## Task 8: `client.py` — endpoint methods

**Files:**
- Modify: `pytossinvest/src/pytossinvest/client.py` (add methods to `TossInvestClient`)
- Modify: `pytossinvest/src/pytossinvest/__init__.py` (exports)
- Test: `pytossinvest/tests/test_client_endpoints.py`

Core endpoints return typed models; thinner ones return the unwrapped `result` (plain dict/list) by design.

- [ ] **Step 1: Write the failing test**

```python
import httpx
import respx

from pytossinvest.client import TossInvestClient
from pytossinvest.models import Account, Price, BuyingPower, OrderResponse
from decimal import Decimal

BASE = "https://openapi.tossinvest.com"


def _token():
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
    )


def _client():
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)


@respx.mock
def test_get_accounts_caches_seq():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    c = _client()
    accounts = c.get_accounts()
    assert isinstance(accounts[0], Account)
    assert c._account_seq == 9  # auto-cached from first account


@respx.mock
def test_get_prices_returns_models():
    _token()
    route = respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(200, json={"result": [{"symbol": "005930", "lastPrice": "70000", "currency": "KRW"}]})
    )
    c = _client()
    prices = c.get_prices(["005930", "000660"])
    assert prices[0].last_price == Decimal("70000")
    assert route.calls.last.request.url.params["symbols"] == "005930,000660"


@respx.mock
def test_get_buying_power():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    respx.get(f"{BASE}/api/v1/buying-power").mock(
        return_value=httpx.Response(200, json={"result": {"currency": "KRW", "cashBuyingPower": "500000"}})
    )
    c = _client()
    c.get_accounts()
    bp = c.get_buying_power("KRW")
    assert isinstance(bp, BuyingPower)
    assert bp.cash_buying_power == Decimal("500000")


@respx.mock
def test_place_order_sends_idempotency_key():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    route = respx.post(f"{BASE}/api/v1/orders").mock(
        return_value=httpx.Response(200, json={"result": {"orderId": "ord-1", "clientOrderId": "cli-1"}})
    )
    c = _client()
    c.get_accounts()
    resp = c.place_order(symbol="005930", side="BUY", order_type="LIMIT",
                         price="70000", quantity="10", client_order_id="cli-1")
    assert isinstance(resp, OrderResponse)
    assert resp.order_id == "ord-1"
    sent = route.calls.last.request
    import json as _json
    payload = _json.loads(sent.content)
    assert payload["clientOrderId"] == "cli-1"
    assert payload["price"] == "70000"  # string, not number


@respx.mock
def test_cancel_order():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    respx.post(f"{BASE}/api/v1/orders/ord-1/cancel").mock(
        return_value=httpx.Response(200, json={"result": {"orderId": "ord-2"}})
    )
    c = _client()
    c.get_accounts()
    out = c.cancel_order("ord-1")
    assert out["orderId"] == "ord-2"


@respx.mock
def test_get_holdings_raw():
    _token()
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 9, "accountType": "BROKERAGE"}]})
    )
    respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json={"result": {"items": [], "marketValue": {"krw": "0"}}})
    )
    c = _client()
    c.get_accounts()
    h = c.get_holdings()
    assert h["items"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_client_endpoints.py -v`
Expected: FAIL with `AttributeError: 'TossInvestClient' object has no attribute 'get_accounts'`.

- [ ] **Step 3: Add endpoint methods to `TossInvestClient`** (append inside the class, after `_request`)

```python
    # --- account / asset ---
    def get_accounts(self) -> list:
        from .models import Account
        result = self._request("GET", "/api/v1/accounts", group="ACCOUNT")
        accounts = [Account.model_validate(a) for a in result]
        if self._account_seq is None and accounts:
            self._account_seq = accounts[0].account_seq
        return accounts

    def get_holdings(self, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/api/v1/holdings", group="ASSET", account=True, params=params)

    # --- market data ---
    def get_prices(self, symbols: list[str]) -> list:
        from .models import Price
        result = self._request(
            "GET", "/api/v1/prices", group="MARKET_DATA",
            params={"symbols": ",".join(symbols)},
        )
        return [Price.model_validate(p) for p in result]

    def get_orderbook(self, symbol: str) -> dict:
        return self._request("GET", "/api/v1/orderbook", group="MARKET_DATA", params={"symbol": symbol})

    def get_trades(self, symbol: str, count: int = 50) -> list:
        return self._request("GET", "/api/v1/trades", group="MARKET_DATA", params={"symbol": symbol, "count": count})

    def get_candles(self, symbol: str, interval: str, count: int = 100, before: str | None = None) -> dict:
        params = {"symbol": symbol, "interval": interval, "count": count}
        if before:
            params["before"] = before
        return self._request("GET", "/api/v1/candles", group="MARKET_DATA_CHART", params=params)

    # --- stock / market info ---
    def get_stocks(self, symbols: list[str]) -> list:
        return self._request("GET", "/api/v1/stocks", group="STOCK", params={"symbols": ",".join(symbols)})

    def get_exchange_rate(self, base: str, quote: str) -> dict:
        return self._request("GET", "/api/v1/exchange-rate", group="MARKET_INFO",
                             params={"baseCurrency": base, "quoteCurrency": quote})

    def get_market_calendar(self, country: str, date: str | None = None) -> dict:
        params = {"date": date} if date else None
        return self._request("GET", f"/api/v1/market-calendar/{country}", group="MARKET_INFO", params=params)

    # --- order info ---
    def get_buying_power(self, currency: str) -> "BuyingPower":
        from .models import BuyingPower
        result = self._request("GET", "/api/v1/buying-power", group="ORDER_INFO",
                              account=True, params={"currency": currency})
        return BuyingPower.model_validate(result)

    def get_sellable_quantity(self, symbol: str) -> dict:
        return self._request("GET", "/api/v1/sellable-quantity", group="ORDER_INFO",
                            account=True, params={"symbol": symbol})

    def get_commissions(self) -> list:
        return self._request("GET", "/api/v1/commissions", group="ORDER_INFO", account=True)

    # --- order history ---
    def list_orders(self, status: str = "OPEN", symbol: str | None = None, cursor: str | None = None, limit: int = 20) -> dict:
        params = {"status": status, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/api/v1/orders", group="ORDER_HISTORY", account=True, params=params)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/orders/{order_id}", group="ORDER_HISTORY", account=True)

    # --- order write ---
    def place_order(self, *, symbol: str, side: str, order_type: str,
                    quantity: str | None = None, price: str | None = None,
                    order_amount: str | None = None, time_in_force: str = "DAY",
                    client_order_id: str | None = None,
                    confirm_high_value_order: bool = False) -> "OrderResponse":
        from .models import OrderResponse
        payload: dict = {"symbol": symbol, "side": side, "orderType": order_type,
                         "timeInForce": time_in_force, "confirmHighValueOrder": confirm_high_value_order}
        if quantity is not None:
            payload["quantity"] = quantity
        if price is not None:
            payload["price"] = price
        if order_amount is not None:
            payload["orderAmount"] = order_amount
        if client_order_id is not None:
            payload["clientOrderId"] = client_order_id
        result = self._request("POST", "/api/v1/orders", group="ORDER", account=True, json=payload)
        return OrderResponse.model_validate(result)

    def modify_order(self, order_id: str, *, order_type: str, price: str | None = None,
                     quantity: str | None = None, confirm_high_value_order: bool = False) -> dict:
        payload: dict = {"orderType": order_type, "confirmHighValueOrder": confirm_high_value_order}
        if price is not None:
            payload["price"] = price
        if quantity is not None:
            payload["quantity"] = quantity
        return self._request("POST", f"/api/v1/orders/{order_id}/modify", group="ORDER", account=True, json=payload)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/api/v1/orders/{order_id}/cancel", group="ORDER", account=True, json={})
```

- [ ] **Step 4: Update `__init__.py` exports**

```python
__version__ = "0.0.1"

from .client import TossInvestClient
from .errors import (
    TossInvestError,
    AuthError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    ConflictError,
    BusinessRuleError,
    RateLimitError,
    ServerError,
    OAuthError,
)
from .money import to_decimal, decimal_to_str

__all__ = [
    "__version__",
    "TossInvestClient",
    "TossInvestError",
    "AuthError",
    "ForbiddenError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "BusinessRuleError",
    "RateLimitError",
    "ServerError",
    "OAuthError",
    "to_decimal",
    "decimal_to_str",
]
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run --package pytossinvest pytest pytossinvest/tests/test_client_endpoints.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add pytossinvest/src/pytossinvest/client.py pytossinvest/src/pytossinvest/__init__.py pytossinvest/tests/test_client_endpoints.py
git commit -m "feat(sdk): endpoint methods (market data, account, order info, orders)"
```

---

## Task 9: Full suite green + README usage

**Files:**
- Modify: `pytossinvest/README.md`
- Test: all

- [ ] **Step 1: Run the full suite**

Run: `uv run --package pytossinvest pytest pytossinvest/tests -v`
Expected: all tests pass (Tasks 1–8: smoke + money + errors + ratelimit + auth + models + client core + endpoints).

- [ ] **Step 2: Add a usage snippet to `README.md`**

````markdown
## Usage

```python
from pytossinvest import TossInvestClient

with TossInvestClient(client_id="...", client_secret="...") as c:
    c.get_accounts()                 # caches accountSeq
    prices = c.get_prices(["005930"])
    print(prices[0].last_price)      # Decimal

    # Orders are string-decimal and idempotent (pass your own clientOrderId)
    c.place_order(symbol="005930", side="BUY", order_type="LIMIT",
                  price="70000", quantity="10", client_order_id="my-001")
```

> Money and quantities are always `Decimal` / strings — never floats.
> The SDK is tested against fixtures; live calls require Toss Open API credentials.
````

- [ ] **Step 3: Commit**

```bash
git add pytossinvest/README.md
git commit -m "docs(sdk): README usage example"
```

---

## Self-Review (completed)

**Spec coverage (SDK portion of §2/§5):** token manager ✓ (Task 5), rate limiter w/ groups + peak-hour ✓ (Task 4), decimal safety ✓ (Task 2/6), error mapping + unknown tolerance ✓ (Task 3), result unwrapping ✓ (Task 7), account header + accountSeq cache ✓ (Task 7/8), idempotency key passthrough ✓ (Task 8), all endpoints from §4 mapped ✓ (Task 8). The MCP-layer items (modes, paper engine, preview/confirm, guardrails, MCP tools, audit log) are **Plan 2**, not this plan.

**Placeholder scan:** no TBD/TODO/"similar to" — every code step has complete code. Progressive typing (thin endpoints return raw `result`) is a stated design decision, not a placeholder.

**Type consistency:** `TossInvestClient` constructor signature, `_request` keyword contract (`group`/`account`/`params`/`json`/`data`), `_account_seq` attribute, model class names (`Account`/`Price`/`BuyingPower`/`OrderResponse`/`HoldingsItem`), and error class names are identical across Tasks 6–9.

**Note on rate-limit backoff:** Task 7 establishes the bucket-paces-then-raises-`RateLimitError` contract. Automatic `Retry-After`/exponential-backoff *retry* orchestration (spec §5) is intentionally deferred — the raised `RateLimitError.retry_after` gives callers what they need, and the MCP layer (Plan 2) decides retry policy. If a built-in retry wrapper is wanted in the SDK, add it as a Task 10 here before starting Plan 2.
