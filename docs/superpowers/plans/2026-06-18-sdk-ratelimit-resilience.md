# SDK 레이트리밋 복원력 Implementation Plan — B3 헤더 동기화 + B4 자동 retry/backoff

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `pytossinvest` 가 응답 `X-RateLimit-*` 헤더로 토큰버킷을 동기화하고(헤더가 진실), 429 를 bounded 자동 재시도(Retry-After/지수백오프+jitter)하되 소진 시 종전대로 `RateLimitError` 를 던지게 한다.

**Architecture:** `client.py` 의 `_request`/`_gate`/생성자와 `ratelimit.py` 의 순수 백오프 헬퍼만 변경. 헤더는 기존 `TokenBucket(capacity, refill_per_sec)` 에 직접 매핑(Limit→capacity, 1/Reset→refill, Remaining→tokens). 모든 헤더 처리는 부재 시 무동작(graceful)이라 헤더 없는 기존 respx mock 무영향. 재시도는 429 한정(미실행이라 POST 도 멱등 안전), 5xx·타임아웃은 비재시도. 공개 API 는 생성자 인자 추가만(비파괴).

**Tech Stack:** Python 3.12, httpx(sync), respx(mock), pytest, uv 워크스페이스.

## Global Constraints

- **돈/수량 float 금지** — 본 작업은 money 미관여지만 어떤 float 진입 경로도 새로 만들지 않는다.
- **AI 작성 표시 금지** — 커밋 메시지·주석·문서 어디에도 AI 생성 표기 금지(`Co-Authored-By` 등). 공개 OSS.
- **SDK 공개 API 비파괴** — 기존 시그니처/반환 타입 불변. 생성자에 **기본값 있는 키워드 인자만** 추가. `tossinvest-mcp` 무변경(무회귀 확인).
- **레이트리밋 표 숫자 하드코딩 금지** — 헤더가 source of truth.
- **재시도 안전 불변식** — 429 만 재시도(요청 미실행 → POST 멱등 안전). 5xx·타임아웃은 재시도하지 않고 그대로 throw. 재시도 시 호출자 `clientOrderId` 불변. 최종 실패는 기존 `RateLimitError` 타입으로 throw.
- **커밋은 각 Task 끝에서만.** 브랜치 `feat/sdk-ratelimit-resilience` 에서 작업.
- **검증 명령**
  - SDK: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q`
  - MCP 무회귀: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q`

**참조 사실(코드 그라운딩):**
- `client.py` `_GROUP_RATES`(MARKET_DATA=10, ORDER=6, ACCOUNT=1 …), 버킷은 그룹별 `TokenBucket(capacity=r, refill_per_sec=r, now=monotonic)`.
- `_gate(group)`: `rate = effective_rate(group, _GROUP_RATES[group], now_kst())`; `bucket.capacity=rate; bucket.refill_per_sec=rate; while not try_acquire: sleep(time_until_available())`.
- `_request(method, path, *, group, account=False, params=None, json=None, data=None, _retried=False)`: `_gate` → 헤더 구성 → `resp = self._http.request(...)` → 200 분기(json/result) → body 파싱 → 401 expired one-shot 재시도 → `raise error_from_response(status, body, resp.headers)`.
- `errors.error_from_response` 가 429 의 `Retry-After` 를 `RateLimitError.retry_after`(float) 로 채움(이미 구현).
- `TokenBucket`(dataclass): 필드 `capacity`, `refill_per_sec`, `now`, 내부 `_tokens`, `_last`. 메서드 `try_acquire(n=1)`, `time_until_available(n=1)`.
- 외부 API 헤더(정상·429 공통): `X-RateLimit-Limit`(capacity) / `X-RateLimit-Remaining`(남은 토큰, 429 시 0) / `X-RateLimit-Reset`(토큰 1개 재충전 초) / `Retry-After`(429 만, 초).
- 테스트 헬퍼(`pytossinvest/tests/test_client_core.py`): `BASE`, `_token_route()`(oauth2/token mock), `_client()` = `TossInvestClient("cid","secret",base_url=BASE, sleep=lambda s: None)`. respx `@respx.mock` + `httpx.Response`.
- `pytossinvest/tests/test_ratelimit.py` 존재(순수 단위 테스트 스타일).

---

## Task 0: 작업 브랜치 + 문서 커밋

**Files:** (git 작업)

- [ ] **Step 1: 브랜치 생성**

```bash
cd /Users/cyj/workspace/personal/toss
git checkout -b feat/sdk-ratelimit-resilience
git status
```
Expected: `On branch feat/sdk-ratelimit-resilience`. 워킹트리에 미커밋 스펙/플랜 문서(`docs/superpowers/specs/2026-06-18-sdk-ratelimit-resilience-design.md`, 본 플랜)가 보임.

- [ ] **Step 2: 스펙+플랜 커밋**

```bash
git add docs/superpowers/specs/2026-06-18-sdk-ratelimit-resilience-design.md docs/superpowers/plans/2026-06-18-sdk-ratelimit-resilience.md
git commit -m "docs: SDK 레이트리밋 복원력 설계+구현 플랜 (B3 헤더 동기화 + B4 retry)"
```

---

## Task 1: `backoff_wait` 순수 함수 (B4 대기 시간 계산)

**Files:**
- Modify: `pytossinvest/src/pytossinvest/ratelimit.py` (`backoff_wait` 추가, `__all__` 갱신)
- Test: `pytossinvest/tests/test_ratelimit.py`

**Interfaces:**
- Produces: `backoff_wait(attempt: int, retry_after: float | None, *, base: float = 1.0, cap: float, rng: Callable[[], float]) -> float`. `retry_after` 가 있으면(>0) 그 값, 없으면 `base * 2**attempt * rng()`(full jitter). 결과를 `cap` 으로 클램프.

- [ ] **Step 1: 실패 테스트** — `test_ratelimit.py` 끝에 추가:

```python
def test_backoff_wait_honors_retry_after():
    from pytossinvest.ratelimit import backoff_wait
    assert backoff_wait(0, 2.0, cap=60.0, rng=lambda: 0.5) == 2.0


def test_backoff_wait_exponential_with_jitter_when_no_retry_after():
    from pytossinvest.ratelimit import backoff_wait
    # base 1 * 2**2 * 0.5 = 2.0
    assert backoff_wait(2, None, base=1.0, cap=60.0, rng=lambda: 0.5) == 2.0
    # attempt 0 -> 1 * 1 * 0.5 = 0.5
    assert backoff_wait(0, None, base=1.0, cap=60.0, rng=lambda: 0.5) == 0.5


def test_backoff_wait_clamps_to_cap():
    from pytossinvest.ratelimit import backoff_wait
    assert backoff_wait(10, None, base=1.0, cap=5.0, rng=lambda: 1.0) == 5.0   # 1024 -> 5
    assert backoff_wait(0, 999.0, cap=60.0, rng=lambda: 0.0) == 60.0           # retry_after clamped


def test_backoff_wait_ignores_nonpositive_retry_after():
    from pytossinvest.ratelimit import backoff_wait
    # retry_after 0 -> fall back to backoff (1 * 2**0 * 0.5 = 0.5)
    assert backoff_wait(0, 0.0, base=1.0, cap=60.0, rng=lambda: 0.5) == 0.5
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package pytossinvest --extra dev pytest "pytossinvest/tests/test_ratelimit.py::test_backoff_wait_honors_retry_after" -v`
Expected: FAIL — `ImportError: cannot import name 'backoff_wait'`.

- [ ] **Step 3: 구현** — `ratelimit.py` 상단 import 에 `Callable` 이 이미 있음(`from typing import Callable`). `__all__` 을 교체:
```python
__all__ = ["TokenBucket", "effective_rate", "backoff_wait", "PEAK_GROUPS"]
```
파일 끝(`effective_rate` 함수 다음)에 추가:
```python
def backoff_wait(
    attempt: int,
    retry_after: "float | None",
    *,
    base: float = 1.0,
    cap: float,
    rng: Callable[[], float],
) -> float:
    """Seconds to wait before a 429 retry. Honors Retry-After (>0); else exponential
    backoff (base * 2**attempt) with full jitter. Clamped to cap to avoid unbounded waits."""
    if retry_after is not None and retry_after > 0:
        wait = retry_after
    else:
        wait = base * (2 ** attempt) * rng()
    return min(wait, cap)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests/test_ratelimit.py -q`
Expected: PASS (신규 4건 포함).

- [ ] **Step 5: Commit**

```bash
git add pytossinvest/src/pytossinvest/ratelimit.py pytossinvest/tests/test_ratelimit.py
git commit -m "feat(ratelimit): backoff_wait helper (Retry-After or exp backoff+jitter, capped)"
```

---

## Task 2: B3 — `X-RateLimit-*` 헤더로 버킷 동기화

**Files:**
- Modify: `pytossinvest/src/pytossinvest/client.py` (`__init__` 헤더-수신 플래그; `_sync_bucket_from_headers`; `_gate` 헤더-우선 분기; `_request` 응답 후 동기화 호출)
- Test: `pytossinvest/tests/test_client_core.py`

**Interfaces:**
- Produces: `TossInvestClient._sync_bucket_from_headers(group: str, headers) -> None` — 세 헤더가 다 있고 파싱되면 그 그룹 버킷의 `capacity=Limit`, `refill_per_sec=1/Reset`(Reset>0), `_tokens=min(_tokens, Remaining)` 갱신하고 `self._rate_from_header[group]=True`. 헤더 누락/파싱불가 → 무동작. `_gate` 는 헤더 수신 그룹엔 정적 `effective_rate` 피크반토막을 적용하지 않는다.

- [ ] **Step 1: 실패 테스트** — `test_client_core.py` 끝에 추가:

```python
@respx.mock
def test_syncs_bucket_from_ratelimit_headers():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        200, json={"result": []},
        headers={"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "2"},
    ))
    c = _client()
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    b = c._buckets["MARKET_DATA"]
    assert b.capacity == 20.0
    assert b.refill_per_sec == 0.5          # 1 / Reset(2)
    assert b._tokens <= 5.0                  # clamped down to Remaining


@respx.mock
def test_no_ratelimit_headers_keeps_static_rate():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(200, json={"result": []}))
    c = _client()
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    c._gate("MARKET_DATA")                   # no header seen -> static default applies
    assert c._buckets["MARKET_DATA"].capacity == 10


@respx.mock
def test_header_rate_overrides_peak_halving():
    from datetime import datetime
    _token_route()
    respx.post(f"{BASE}/api/v1/orders").mock(return_value=httpx.Response(
        200, json={"result": {"orderId": "1"}},
        headers={"X-RateLimit-Limit": "6", "X-RateLimit-Remaining": "6", "X-RateLimit-Reset": "1"},
    ))
    peak = datetime(2026, 6, 17, 9, 5)       # inside 09:00-09:10 peak window
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, now_kst=lambda: peak)
    c._account_seq = 1
    c._request("POST", "/api/v1/orders", group="ORDER", account=True, json={})
    c._gate("ORDER")                         # header seen -> must NOT re-halve 6 to 3
    assert c._buckets["ORDER"].capacity == 6.0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package pytossinvest --extra dev pytest "pytossinvest/tests/test_client_core.py::test_syncs_bucket_from_ratelimit_headers" "pytossinvest/tests/test_client_core.py::test_header_rate_overrides_peak_halving" -v`
Expected: FAIL — 헤더 미반영(capacity 10 유지 / 피크에서 3 으로 반토막).

- [ ] **Step 3: 구현 (a) `__init__` 플래그** — `client.py` `__init__` 의 `self._account_seq: int | None = None` 다음 줄에 추가:
```python
        self._rate_from_header: dict[str, bool] = {}
```

- [ ] **Step 4: 구현 (b) `_gate` 헤더-우선 분기** — 현재 `_gate` 본문을 교체:
```python
    def _gate(self, group: str) -> None:
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        # Once the server's X-RateLimit-* headers have been seen for this group, they are
        # the source of truth (they already reflect the 09:00-09:10 peak halving), so don't
        # re-apply the static effective_rate. Until then, use the documented default.
        if not self._rate_from_header.get(group):
            rate = effective_rate(group, _GROUP_RATES[group], self._now_kst())
            bucket.capacity = rate
            bucket.refill_per_sec = rate
        while not bucket.try_acquire():
            self._sleep(bucket.time_until_available())
```

- [ ] **Step 5: 구현 (c) `_sync_bucket_from_headers`** — `_gate` 메서드 바로 다음에 추가:
```python
    def _sync_bucket_from_headers(self, group: str, headers) -> None:
        """Reconcile a group's bucket to the server's X-RateLimit-* headers (source of truth).
        No-op if any header is absent or unparseable."""
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        limit = headers.get("X-RateLimit-Limit")
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if limit is None or remaining is None or reset is None:
            return
        try:
            limit_f = float(limit)
            remaining_f = float(remaining)
            reset_f = float(reset)
        except (TypeError, ValueError):
            return
        bucket.capacity = limit_f
        if reset_f > 0:
            bucket.refill_per_sec = 1.0 / reset_f
        bucket._tokens = min(bucket._tokens, remaining_f)
        self._rate_from_header[group] = True
```

- [ ] **Step 6: 구현 (d) `_request` 응답 후 동기화** — `_request` 에서 `resp = self._http.request(...)` 호출 **바로 다음 줄**에 삽입(200/429/기타 모든 분기 전):
```python
        self._sync_bucket_from_headers(group, resp.headers)
```

- [ ] **Step 7: 통과 확인**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests/test_client_core.py -q`
Expected: PASS (신규 3건 포함). 기존 `test_gate_applies_peak_hour_halving` 무회귀(헤더 미수신이라 `_rate_from_header` 비어 정적+반토막 유지).

- [ ] **Step 8: Commit**

```bash
git add pytossinvest/src/pytossinvest/client.py pytossinvest/tests/test_client_core.py
git commit -m "feat(client): sync token bucket from X-RateLimit-* headers (header is source of truth)"
```

---

## Task 3: B4 — 429 자동 재시도 (bounded, 기본 켬)

**Files:**
- Modify: `pytossinvest/src/pytossinvest/client.py` (`import random`; `backoff_wait` import; 생성자 `max_retries`/`retry_max_wait`/`rng`; `_request` 429 재시도 루프 + `_attempt` 인자)
- Test: `pytossinvest/tests/test_client_core.py` (기존 429 테스트 교체 + 신규)

**Interfaces:**
- Consumes: Task 1 `backoff_wait`, Task 2 `_sync_bucket_from_headers`(이미 헤더 반영).
- Produces: `TossInvestClient(..., max_retries: int = 3, retry_max_wait: float = 60.0, rng: Callable[[], float] = random.random)`. `_request(..., _attempt: int = 0)`. 429 수신 & `_attempt < max_retries` 이면 `backoff_wait` 만큼 `self._sleep` 후 `_attempt+1` 로 재시도; 소진 시 `RateLimitError` throw. 5xx·기타는 재시도 없음.

- [ ] **Step 1: 실패/교체 테스트** — `test_client_core.py` 의 기존 `test_429_raises_rate_limit_error`(73–82행)를 아래로 **교체**하고, 이어서 신규 테스트들을 추가:

```python
@respx.mock
def test_max_retries_zero_raises_immediately():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "2"}))
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, max_retries=0)
    with pytest.raises(RateLimitError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert route.call_count == 1                 # no retry
    assert exc.value.retry_after == 2.0


@respx.mock
def test_429_then_200_retries_to_success():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(side_effect=[
        httpx.Response(429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"result": []}),
    ])
    c = _client()                                # default max_retries=3, sleep no-op
    result = c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert result == []
    assert route.call_count == 2


@respx.mock
def test_429_honors_retry_after_in_sleep():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(side_effect=[
        httpx.Response(429, json={"error": {}}, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"result": []}),
    ])
    slept = []
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: slept.append(s))
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert slept == [2.0]


@respx.mock
def test_429_backoff_jitter_when_no_retry_after():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(side_effect=[
        httpx.Response(429, json={"error": {}}),    # no Retry-After
        httpx.Response(200, json={"result": []}),
    ])
    slept = []
    c = TossInvestClient("cid", "secret", base_url=BASE,
                         sleep=lambda s: slept.append(s), rng=lambda: 0.5)
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert slept == [0.5]                         # 1 * 2**0 * 0.5


@respx.mock
def test_429_exhausts_retries_then_raises():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "1"}))
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, max_retries=2)
    with pytest.raises(RateLimitError):
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert route.call_count == 3                  # 1 initial + 2 retries


@respx.mock
def test_5xx_is_not_retried():
    from pytossinvest.errors import ServerError
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        503, json={"error": {"code": "server-error"}}))
    c = _client()                                # default max_retries=3
    with pytest.raises(ServerError):
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert route.call_count == 1                  # no retry on 5xx


@respx.mock
def test_429_retry_preserves_client_order_id_on_post():
    import json as _json
    _token_route()
    route = respx.post(f"{BASE}/api/v1/orders").mock(side_effect=[
        httpx.Response(429, json={"error": {}}, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"result": {"orderId": "1", "clientOrderId": "abc"}}),
    ])
    c = _client()
    c._account_seq = 1
    c.place_order(symbol="005930", side="BUY", order_type="LIMIT",
                  quantity="1", price="100", client_order_id="abc")
    assert route.call_count == 2
    for call in route.calls:
        assert _json.loads(call.request.content)["clientOrderId"] == "abc"  # idempotent retry
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package pytossinvest --extra dev pytest "pytossinvest/tests/test_client_core.py::test_429_then_200_retries_to_success" "pytossinvest/tests/test_client_core.py::test_max_retries_zero_raises_immediately" -v`
Expected: FAIL — 재시도 미구현(`TypeError: unexpected keyword 'max_retries'`).

- [ ] **Step 3: 구현 (a) import** — `client.py` 상단:
  - `import time as _time` 아래(또는 표준 import 그룹)에 `import random` 추가.
  - `from .ratelimit import TokenBucket, effective_rate` → `from .ratelimit import TokenBucket, effective_rate, backoff_wait`.

- [ ] **Step 4: 구현 (b) 생성자 인자** — `__init__` 시그니처에 `now_kst: Callable[[], datetime] = lambda: datetime.now(_KST),` 다음 줄에 추가:
```python
        max_retries: int = 3,
        retry_max_wait: float = 60.0,
        rng: Callable[[], float] = random.random,
```
그리고 본문(`self._now_kst = now_kst` 다음)에 저장:
```python
        self._max_retries = max_retries
        self._retry_max_wait = retry_max_wait
        self._rng = rng
```

- [ ] **Step 5: 구현 (c) `_request` 시그니처에 `_attempt`** — `_request` 의 `_retried: bool = False,` 다음 줄에 추가:
```python
        _attempt: int = 0,
```
그리고 401 재시도 재귀 호출(`return self._request(... _retried=True,)`)에 `_attempt=_attempt,` 를 추가(401 재시도가 429 카운터를 보존):
```python
            return self._request(
                method, path, group=group, account=account,
                params=params, json=json, data=data, _retried=True, _attempt=_attempt,
            )
```

- [ ] **Step 6: 구현 (d) 429 재시도 블록** — `_request` 의 마지막 `raise error_from_response(resp.status_code, body, resp.headers)` **바로 앞**에 삽입:
```python
        if resp.status_code == 429 and _attempt < self._max_retries:
            retry_after = None
            raw = resp.headers.get("Retry-After")
            if raw is not None:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
            self._sleep(backoff_wait(_attempt, retry_after,
                                     cap=self._retry_max_wait, rng=self._rng))
            return self._request(
                method, path, group=group, account=account,
                params=params, json=json, data=data, _retried=_retried, _attempt=_attempt + 1,
            )

```

- [ ] **Step 7: 통과 확인**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests/test_client_core.py -q`
Expected: PASS (신규 7건 포함). 기존 401/200/4xx/5xx/peak 테스트 무회귀(5xx 는 재시도 안 함, 401 one-shot 유지).

- [ ] **Step 8: 전체 무회귀**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q`
Expected: 전부 PASS.
Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q`
Expected: 전부 PASS (MCP 무변경 확인).

- [ ] **Step 9: Commit**

```bash
git add pytossinvest/src/pytossinvest/client.py pytossinvest/tests/test_client_core.py
git commit -m "feat(client): bounded auto-retry on 429 (Retry-After/backoff+jitter); 5xx not retried"
```

---

## Task 4: 문서 동기화 (SDK 규약 + docs/claude + README + 테스트 수)

**Files:**
- Modify: `CLAUDE.md` (Conventions SDK 규약의 v0.0.1 한계 문구)
- Modify: `docs/claude/pytossinvest-sdk.md` (`ratelimit.py` + `_request` 절)
- Modify: `pytossinvest/README.md` (레이트리밋 한계 문구)
- Modify: 테스트 수 표기(`CLAUDE.md` Commands)

**Interfaces:** (문서만)

- [ ] **Step 1: 최종 테스트 수 확인**

```bash
uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q 2>&1 | tail -1
uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q 2>&1 | tail -1
```
SDK 신규 수 기록(기존 46 + Task1 4 + Task2 3 + Task3 [기존 1 교체 → 신규 7, 순증 6] = 약 59). 실제 출력값 사용. MCP 는 109 무변경.

- [ ] **Step 2: CLAUDE.md SDK 규약 갱신** — Conventions 의 SDK 규약 줄에서
> v0.0.1 한계: 헤더 동적 동기화·자동 retry/backoff 미구현(`RateLimitError.retry_after` 던짐 → 호출자/ MCP 책임).

를 교체:
> v0.0.2: `X-RateLimit-*` 헤더로 버킷 동적 동기화(헤더가 진실 — 본 그룹은 피크반토막 미적용), 429 **bounded 자동 retry**(`Retry-After` 또는 지수백오프+jitter, `max_retries` 기본 3, `retry_max_wait` 60s 상한) 구현. **5xx·타임아웃은 비재시도**(호출자 책임). 소진 시 종전대로 `RateLimitError` throw.

- [ ] **Step 3: CLAUDE.md Commands 테스트 수** — `SDK (46)` 표기를 Step 1 의 실제 수로 갱신(예: `SDK (59)`), 총계도 갱신(SDK+MCP).

- [ ] **Step 4: docs/claude/pytossinvest-sdk.md 갱신** — `ratelimit.py` 항목의 "⚠ v0.0.1 은 정적 기본값 + 피크반토막만 — 헤더 동적 동기화·자동 retry/backoff 미구현" 문구를, 구현됨으로 갱신: `backoff_wait` 헬퍼 추가; `_sync_bucket_from_headers`(Limit→capacity, 1/Reset→refill, min(_tokens,Remaining), 그룹별 `_rate_from_header` 플래그 → 헤더 본 그룹은 `_gate` 가 피크반토막 미적용); `_request` 가 응답마다 헤더 동기화 + 429 한정 bounded 재시도(`_attempt`, `backoff_wait`, `self._sleep`), 5xx 비재시도; 생성자 `max_retries`/`retry_max_wait`/`rng`. `_request` 오케스트레이션 설명에 429 재시도 단계 반영.

- [ ] **Step 5: pytossinvest/README.md 갱신** — "레이트리밋은 정적 기본값 + 피크 반토막만 … 동적 동기화 … 미구현(v0.0.2 예정)" 문단을 구현됨으로 갱신(헤더 동기화 + 429 자동 retry, 5xx 비재시도, 생성자 노브). 테스트 수 표기 있으면 갱신.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md docs/claude/pytossinvest-sdk.md pytossinvest/README.md
git commit -m "docs: SDK rate-limit dynamic header sync + bounded 429 retry"
```

---

## 완료 기준

- `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q` 전부 그린.
- `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q` 전부 그린(MCP 무회귀).
- B3: 응답 헤더로 버킷 동기화(Limit/1·Reset/Remaining), 헤더 없으면 정적 유지, 헤더 본 그룹은 피크반토막 미적용.
- B4: 429 bounded 자동 재시도(Retry-After/백오프+jitter, cap), `max_retries=0` 즉시 throw, 소진 시 `RateLimitError`, 5xx 비재시도, POST 재시도 시 `clientOrderId` 불변.
- 공개 API 비파괴(생성자 인자 추가만), 문서 동기화.

## 영향 파일 요약

| 파일 | Task |
|---|---|
| `pytossinvest/src/pytossinvest/ratelimit.py` | 1(backoff_wait) |
| `pytossinvest/src/pytossinvest/client.py` | 2(헤더 동기화·_gate 분기), 3(429 재시도·생성자 노브) |
| `pytossinvest/tests/test_ratelimit.py` | 1 |
| `pytossinvest/tests/test_client_core.py` | 2, 3 |
| `CLAUDE.md`, `docs/claude/pytossinvest-sdk.md`, `pytossinvest/README.md` | 4 |
