# MCP 안전 회계 Implementation Plan — C1 권위 통화 + M1 modify 델타 회계

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** preview 시점에 API 의 권위 통화로 가드레일 임계·버킷을 고르고(실패 시 심볼모양 폴백), modify 가 일일 버킷에 완전 델타 회계(증가 검사+가산, 감소 credit, 0 하한)를 적용하며 재부팅 너머 복원되게 한다.

**Architecture:** `tossinvest-mcp` 의 `safety.py`(SafetyManager 회계·검증)와 `tools.py`(preview/place/modify 오케스트레이션)만 변경. SDK(`pytossinvest`) 공개 API 불변. 통화 결정은 preview 에서 `get_prices([symbol])` 한 번으로 얻고 `build_spec(currency=...)` 로 주입. modify 는 `check_daily=False`+`release` 에서 `check_daily=True`(델타)+`finalize(델타)` 로 전환.

**Tech Stack:** Python 3.12, pydantic v2, pytest, uv 워크스페이스. 돈/수량은 전구간 `Decimal`/문자열(`pytossinvest.money.to_decimal`).

## Global Constraints

- **돈/수량 float 금지** — 전구간 `Decimal`/문자열. `to_decimal` 이 float 를 `TypeError` 로 거부.
- **AI 작성 표시 금지** — 커밋 메시지·주석·문서 어디에도 AI 생성 표기 금지(`Co-Authored-By` 등). 공개 OSS.
- **SDK 공개 API 불변** — `pytossinvest` 시그니처/반환 타입 변경 금지. 이 플랜은 SDK 미변경.
- **place 안전 불변식 유지** — 체결 경로는 반드시 `check_guardrails` 경유. preview→place: consume→실행→성공시 finalize. modify→ 본 플랜으로 consume→가드레일(델타)→실행→성공시 finalize(델타).
- **커밋은 각 Task 끝에서만**(별도 푸시/머지는 안 함). 브랜치: `feat/mcp-safety-accounting` 에서 작업.
- **통화 비교는 주문통화 기준, FX 환산 없음** — KRW/USD 버킷 분리 유지.
- **테스트 import 규약** — `from conftest import ...` (pytest 가 tests/ 를 sys.path 에 넣음). `from tests.conftest` 금지.
- **검증 명령**
  - MCP: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q`
  - SDK 무회귀: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q`

**참조 사실(코드 그라운딩):**
- `safety.order_currency(symbol)` = `"USD" if symbol.isalpha() else "KRW"` (폴백으로 잔존).
- `build_spec` 는 이미 `currency=order_currency(symbol)` 로 `OrderSpec.currency` 설정. `modify_order_id` 인자 존재.
- `check_guardrails(spec, *, is_market_open, enforce_hours, check_daily=True)` 순서: deny/allow → hard_ceiling → high_value → per_order_cap → daily → hours.
- `record_spend(notional, currency="KRW")`, `finalize(token, notional)`(pop+record), `release(token)`(pop only — **제거 대상**).
- `restore_spend(events)` 는 `decision=="placed"` 의 `notional` 만 통화별 합산(UTC ts→KST date 당일만).
- `AuditLog.record` 는 `json.dumps(..., default=str)` → `Decimal` 은 `"700000"`/`"-100"` 문자열로 직렬화. `read_events` 는 bad line skip.
- `tools._ref_price(app, symbol)` = `str(get_prices([symbol])[0].last_price)` or None. (place_order 의 paper MARKET 체결에 사용 — 유지)
- `Price` 모델: `symbol: str`, `last_price: Money(alias lastPrice)`, `currency: str`(필수).
- conftest `FakeClient.get_prices` 는 모든 심볼에 `currency="KRW", lastPrice="70000"` 반환. `get_order` 는 `{symbol:"005930", side:"BUY", orderType:"LIMIT", quantity:"10", price:"70000", status:"PENDING"}`.

---

## Task 0: 작업 브랜치 생성

**Files:** (없음 — git 작업만)

- [ ] **Step 1: 브랜치 생성**

```bash
cd /Users/cyj/workspace/personal/toss
git checkout -b feat/mcp-safety-accounting
git status
```
Expected: `On branch feat/mcp-safety-accounting`, 워킹트리에 미커밋 스펙/플랜 문서가 보임(`docs/superpowers/specs/2026-06-17-mcp-safety-accounting-design.md`, 본 플랜).

- [ ] **Step 2: 스펙+플랜 문서 커밋**

```bash
git add docs/superpowers/specs/2026-06-17-mcp-safety-accounting-design.md docs/superpowers/plans/2026-06-17-mcp-safety-accounting.md
git commit -m "docs: MCP 안전회계 설계+구현 플랜 (C1 권위통화 + M1 델타회계)"
```
Expected: 커밋 생성.

---

## Task 1: C1a — `build_spec` 가 명시적 `currency` 를 받음

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`build_spec` 시그니처/본문)
- Test: `tossinvest-mcp/tests/test_safety_guardrails.py`

**Interfaces:**
- Produces: `SafetyManager.build_spec(..., currency: str | None = None)`. `currency` 가 주어지면 `OrderSpec.currency` 로 사용, `None` 이면 `order_currency(symbol)` 폴백. 다른 인자/반환 불변.

- [ ] **Step 1: 실패 테스트** — `test_safety_guardrails.py` 끝에 추가:

```python
def test_build_spec_explicit_currency_overrides_symbol_shape():
    m = _mgr()
    # numeric symbol would default to KRW, but explicit currency wins
    spec = m.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="1", price="100", currency="USD")
    assert spec.currency == "USD"


def test_build_spec_currency_none_falls_back_to_symbol_shape():
    m = _mgr()
    spec = m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET",
                        order_amount="100", currency=None)
    assert spec.currency == "USD"  # symbol-shape fallback
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_safety_guardrails.py::test_build_spec_explicit_currency_overrides_symbol_shape" -v`
Expected: FAIL — `build_spec() got an unexpected keyword argument 'currency'`.

- [ ] **Step 3: 구현** — `safety.py::build_spec` 시그니처에 `currency` 추가(마지막 키워드 인자로):

`def build_spec(` 의 인자 목록에서 `modify_order_id: "str | None" = None,` 다음 줄에 추가:
```python
        currency: "str | None" = None,
```
그리고 `return OrderSpec(` 블록의 `currency=order_currency(symbol),` 줄을 교체:
```python
            currency=currency if currency is not None else order_currency(symbol),
```

- [ ] **Step 4: 통과 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py -q`
Expected: PASS (신규 2건 포함 전부 그린 — 기존 `test_build_spec_sets_currency_and_modify_id` 무회귀: currency 미전달 시 폴백 유지).

- [ ] **Step 5: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/tests/test_safety_guardrails.py
git commit -m "feat(safety): build_spec accepts explicit currency (falls back to symbol shape)"
```

---

## Task 2: C1b — preview 툴이 API 권위 통화를 조회·주입 (실패시 폴백)

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/tools.py` (`_price_and_currency` 헬퍼 추가; `preview_order`/`preview_modify` 통화 조회+주입)
- Test: `tossinvest-mcp/tests/test_tools_write.py`

**Interfaces:**
- Consumes: Task 1 `build_spec(currency=...)`.
- Produces: `tools._price_and_currency(app, symbol) -> tuple[str | None, str | None]` — `get_prices([symbol])` 한 번으로 `(last_price_str|None, currency|None)` 반환, 예외/빈결과/공백통화는 `(None, None)` 류로 강등. `preview_order`/`preview_modify` 는 이 한 번의 호출 결과로 통화를 `build_spec` 에 주입하고, MARKET+무금액 경로의 ref price 도 같은 결과에서 취함(중복 get_prices 호출 없음).

- [ ] **Step 1: 실패 테스트** — `test_tools_write.py` 끝에 추가:

```python
def test_preview_uses_authoritative_currency_from_api(app_factory, fake_client):
    app = app_factory(mode="paper")
    from pytossinvest.models import Price
    # numeric symbol that the API says is actually USD-denominated
    fake_client.get_prices = lambda symbols: [
        Price.model_validate({"symbol": symbols[0], "lastPrice": "100", "currency": "USD"})
    ]
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="1", price="100")
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    placed = [json.loads(l) for l in open(app.config.audit_log_path, encoding="utf-8")
              if json.loads(l)["decision"] == "placed"][0]
    assert placed["currency"] == "USD"  # authoritative, not symbol-shape KRW


def test_preview_falls_back_to_symbol_shape_when_price_lookup_fails(app_factory, fake_client):
    app = app_factory(mode="paper")
    def boom(symbols):
        raise RuntimeError("market data down")
    fake_client.get_prices = boom
    pv = T.preview_order(app, symbol="AAPL", side="BUY", order_type="LIMIT",
                         quantity="1", price="100")
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    placed = [json.loads(l) for l in open(app.config.audit_log_path, encoding="utf-8")
              if json.loads(l)["decision"] == "placed"][0]
    assert placed["currency"] == "USD"  # AAPL -> symbol-shape fallback


def test_market_preview_uses_single_price_call(app_factory, fake_client):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="MARKET", quantity="10")
    n = sum(1 for c in fake_client.calls if c[0] == "get_prices")
    assert n == 1  # currency + ref price share one call
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_tools_write.py::test_preview_uses_authoritative_currency_from_api" "tossinvest-mcp/tests/test_tools_write.py::test_market_preview_uses_single_price_call" -v`
Expected: FAIL — 첫 테스트는 currency=="KRW"(아직 심볼모양), MARKET 테스트는 get_prices 호출 0회(LIMIT 은 호출 안 함 / MARKET 은 _ref_price 1회지만 currency 주입 미구현이라 첫 테스트가 핵심 실패).

- [ ] **Step 3: 구현 (a) 헬퍼** — `tools.py` 의 `_ref_price` 함수(현재 130–132행) 바로 아래에 추가:

```python
def _price_and_currency(app: AppContext, symbol: str) -> "tuple[str | None, str | None]":
    """One get_prices call -> (last_price, currency). Tolerates failure for graceful fallback."""
    try:
        prices = app.client.get_prices([symbol])
    except Exception:
        return None, None
    if not prices:
        return None, None
    p = prices[0]
    last = str(p.last_price) if p.last_price is not None else None
    cur = (p.currency or "").strip() or None
    return last, cur
```

- [ ] **Step 4: 구현 (b) preview_order** — `preview_order` 본문 상단(현재 155–157행)의
```python
    ref = None
    if order_type == "MARKET" and order_amount is None:
        ref = _ref_price(app, symbol)
```
를 교체:
```python
    last, currency = _price_and_currency(app, symbol)
    ref = last if (order_type == "MARKET" and order_amount is None) else None
```
그리고 같은 함수의 `build_spec(` 호출에 `currency=currency,` 추가(예: `ref_price=ref,` 다음):
```python
        ref_price=ref, currency=currency,
```

- [ ] **Step 5: 구현 (c) preview_modify** — `preview_modify` 의 `build_spec(` 호출 직전(현재 244행 앞)에 통화 조회 추가하고 호출에 currency 주입. 현재
```python
    spec = app.safety.build_spec(
        symbol=symbol, side=side, order_type=order_type,
        quantity=merged_qty, price=merged_price,
        confirm_high_value_order=confirm_high_value_order, modify_order_id=order_id,
    )
```
를 교체:
```python
    _, currency = _price_and_currency(app, symbol)
    spec = app.safety.build_spec(
        symbol=symbol, side=side, order_type=order_type,
        quantity=merged_qty, price=merged_price,
        confirm_high_value_order=confirm_high_value_order, modify_order_id=order_id,
        currency=currency,
    )
```

- [ ] **Step 6: 통과 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_write.py -q`
Expected: PASS (신규 3건 포함). 기존 preview/place/modify 테스트 무회귀(FakeClient 기본 통화 KRW 라 "005930" 경로 동일).

- [ ] **Step 7: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/tests/test_tools_write.py
git commit -m "feat(tools): resolve authoritative order currency at preview (single price call, symbol-shape fallback)"
```

---

## Task 3: M1a — `OrderSpec.prev_notional` + `check_guardrails` 델타 일일검사

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`OrderSpec` 필드; `check_guardrails` 시그니처/일일블록)
- Test: `tossinvest-mcp/tests/test_safety_guardrails.py`

**Interfaces:**
- Produces: `OrderSpec.prev_notional: Decimal | None = None`. `check_guardrails(..., prev_notional: Decimal | None = None)` — 일일블록 증분이 `prev_notional is None` 이면 `spec.notional`, 아니면 `spec.notional - prev_notional`. per-order/high-value/hard-ceiling 은 여전히 `spec.notional` 전액.

- [ ] **Step 1: 실패 테스트** — `test_safety_guardrails.py` 끝에 추가:

```python
def test_daily_check_uses_delta_when_prev_notional_given():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    m.record_spend(Decimal("950000"), "KRW")  # bucket near cap
    # amended order: new=710,000, prev=700,000 -> delta=+10,000 -> 960,000 <= 1,000,000 OK
    spec = _spec(m, quantity="10", price="71000")  # notional 710,000
    m.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                       prev_notional=Decimal("700000"))  # must NOT raise


def test_daily_check_delta_still_rejects_when_over_cap():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    m.record_spend(Decimal("950000"), "KRW")
    # new=900,000, prev=100,000 -> delta=+800,000 -> 1,750,000 > 1,000,000 -> reject
    spec = _spec(m, quantity="10", price="90000")  # notional 900,000
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                           prev_notional=Decimal("100000"))
    assert e.value.code == "daily-limit"


def test_per_order_cap_uses_full_notional_not_delta():
    m = _mgr(max_order_amount="500000", daily_order_limit="999999999")
    # delta tiny but full new notional exceeds per-order cap
    spec = _spec(m, quantity="10", price="71000")  # 710,000 > 500,000 cap
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                           prev_notional=Decimal("700000"))
    assert e.value.code == "order-amount-cap"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_safety_guardrails.py::test_daily_check_uses_delta_when_prev_notional_given" -v`
Expected: FAIL — `check_guardrails() got an unexpected keyword argument 'prev_notional'`.

- [ ] **Step 3: 구현 (a) OrderSpec 필드** — `safety.py` 의 `OrderSpec` 데이터클래스에서 `modify_order_id: "str | None" = None` 다음 줄에 추가:

```python
    prev_notional: "Decimal | None" = None
```

- [ ] **Step 4: 구현 (b) check_guardrails 시그니처** — 현재
```python
    def check_guardrails(
        self, spec: OrderSpec, *, is_market_open: bool, enforce_hours: bool,
        check_daily: bool = True,
    ) -> None:
```
를 교체:
```python
    def check_guardrails(
        self, spec: OrderSpec, *, is_market_open: bool, enforce_hours: bool,
        check_daily: bool = True, prev_notional: "Decimal | None" = None,
    ) -> None:
```

- [ ] **Step 5: 구현 (c) 일일블록 델타** — `check_guardrails` 의 `if check_daily:` 블록을 교체:
```python
        if check_daily:
            self._roll_daily()
            increment = spec.notional if prev_notional is None else spec.notional - prev_notional
            if self._spent[spec.currency] + increment > daily_cap:
                raise GuardrailError(
                    "daily-limit",
                    f"this order would push today's {spec.currency} total over {daily_cap}",
                )
```

- [ ] **Step 6: 통과 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py -q`
Expected: PASS (신규 3건 포함, 기존 daily 테스트 무회귀 — prev_notional 미전달이면 종전과 동일).

- [ ] **Step 7: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/tests/test_safety_guardrails.py
git commit -m "feat(safety): delta-aware daily check via prev_notional (per-order/high-value still use full notional)"
```

---

## Task 4: M1b — `record_spend` 0-하한 + `release` 제거 + modify 가 델타를 `finalize`

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`record_spend` 0-하한; `release` 제거)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/tools.py` (`preview_modify`/`modify_order` 델타 회계)
- Test: `tossinvest-mcp/tests/test_safety_tokens.py` (release 테스트 제거, 0-하한 테스트 추가), `tossinvest-mcp/tests/test_tools_write.py` (modify 누적 테스트 갱신)

**Interfaces:**
- Consumes: Task 3 `OrderSpec.prev_notional`, `check_guardrails(prev_notional=...)`.
- Produces: `record_spend` 가 통화 버킷을 `max(0, …)` 로 하한. `SafetyManager.release` **삭제**. `preview_modify` 가 원본 명목금액을 `spec.prev_notional` 로 설정하고 `check_daily=True, prev_notional=…` 로 검사. `modify_order` 가 성공 시 `delta = spec.notional - (spec.prev_notional or 0)` 를 `finalize(token, delta)` 로 가산.

- [ ] **Step 1: 실패 테스트 (safety)** — `test_safety_tokens.py` 에서 `test_release_pops_without_recording_spend` 함수(108–117행) **전체 삭제**, 그 자리에 추가:

```python
def test_record_spend_floors_at_zero():
    clock = Clock()
    m = _mgr(clock, daily_order_limit="999999999")
    m.record_spend(Decimal("100000"), "KRW")
    m.record_spend(Decimal("-300000"), "KRW")  # over-credit (modify downsize)
    assert m._spent["KRW"] == Decimal("0")  # floored, never negative
```

- [ ] **Step 2: 실패 테스트 (tools)** — `test_tools_write.py` 의 `test_modify_does_not_touch_daily_bucket`(140–144행)을 교체:

```python
def test_modify_accrues_delta_to_daily_bucket(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    # original real-1: 70000 * 10 = 700,000 ; modify price -> 71000 => 710,000 ; delta +10,000
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert app.safety._spent["KRW"] == Decimal("10000")  # M1: delta accrued


def test_modify_downsize_credits_with_floor(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    app.safety.record_spend(Decimal("700000"), "KRW")  # prior bucket
    # original real-1 = 700,000 ; modify down to 60000*10 = 600,000 ; delta -100,000
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="60000")
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert app.safety._spent["KRW"] == Decimal("600000")  # 700,000 - 100,000 (credited)
```

- [ ] **Step 3: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_tools_write.py::test_modify_accrues_delta_to_daily_bucket" "tossinvest-mcp/tests/test_safety_tokens.py::test_record_spend_floors_at_zero" -v`
Expected: FAIL — modify 는 아직 가산 안 함(`_spent==0`); record_spend 는 음수 허용해 `-200000`.

- [ ] **Step 4: 구현 (a) record_spend 0-하한** — `safety.py::record_spend` 본문 교체:
```python
    def record_spend(self, notional: Decimal, currency: str = "KRW") -> None:
        self._roll_daily()
        self._spent[currency] = max(
            Decimal("0"), self._spent.get(currency, Decimal("0")) + notional
        )
```

- [ ] **Step 5: 구현 (b) release 제거** — `safety.py` 끝의 `release` 메서드(현재 239–241행) **전체 삭제**:
```python
    def release(self, token: str) -> None:
        """Drop a pending token without recording spend (modify: per-order gated, no daily bucket)."""
        self._pending.pop(token, None)
```

- [ ] **Step 6: 구현 (c) preview_modify 델타 설정** — `tools.py::preview_modify` 에서 (Task 2 로 currency 주입된) `build_spec(...)` 호출 다음에 원본 명목금액 계산을 추가하고, `check_guardrails` 호출을 델타 검사로 교체. 현재(Task 2 적용 후):
```python
    is_open, enforce = _market_gate(app, symbol)
    app.safety.check_guardrails(spec, is_market_open=is_open, enforce_hours=enforce,
                                check_daily=False)  # M1: per-order gated, no daily bucket
```
를 교체:
```python
    orig_price = original.get("price")
    orig_qty = original.get("quantity")
    if orig_price is not None and orig_qty is not None:
        spec.prev_notional = to_decimal(orig_price) * to_decimal(orig_qty)
    is_open, enforce = _market_gate(app, symbol)
    app.safety.check_guardrails(spec, is_market_open=is_open, enforce_hours=enforce,
                                check_daily=True, prev_notional=spec.prev_notional)
```

- [ ] **Step 7: 구현 (d) modify_order 델타 회계** — `tools.py::modify_order` 에서 `consume` 직후 재검사와 성공 처리를 교체. 현재:
```python
    spec = app.safety.consume(confirmation_token)  # validates exists + not expired
    # re-check amount guardrails on the amended order (M1: no daily bucket add/check)
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=False)
```
를 교체:
```python
    spec = app.safety.consume(confirmation_token)  # validates exists + not expired
    # re-check amount guardrails on the amended order against the delta (M1: delta accounting)
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False,
                                check_daily=True, prev_notional=spec.prev_notional)
```
그리고 성공 처리부 — 현재:
```python
    app.safety.release(confirmation_token)  # pop only, no daily accrual (M1)
    app.audit.record({
        "tool": "modify_order", "mode": app.config.mode, "decision": "modified",
        "orderId": spec.modify_order_id, "result": result,
        "clientOrderId": spec.client_order_id,
    })
```
를 교체:
```python
    delta = spec.notional - (spec.prev_notional or Decimal("0"))
    app.safety.finalize(confirmation_token, delta)  # pop + accrue signed delta (floored)
    app.audit.record({
        "tool": "modify_order", "mode": app.config.mode, "decision": "modified",
        "orderId": spec.modify_order_id, "result": result,
        "clientOrderId": spec.client_order_id,
        "notional": delta, "currency": spec.currency,
    })
```

- [ ] **Step 8: 통과 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_write.py tossinvest-mcp/tests/test_safety_tokens.py -q`
Expected: PASS. `test_preview_then_modify_calls_client_and_releases_token` 도 그린(finalize 가 토큰을 pop → 2차 modify 실패 유지).

- [ ] **Step 9: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/tests/test_safety_tokens.py tossinvest-mcp/tests/test_tools_write.py
git commit -m "feat(safety): modify applies full delta accounting to daily bucket (floored); remove release"
```

---

## Task 5: M1c — 감사 델타 기록 + `restore_spend` 가 `modified` 합산

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`restore_spend`)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/tools.py` (`preview_modify` 감사에 currency)
- Test: `tossinvest-mcp/tests/test_safety_tokens.py`

**Interfaces:**
- Consumes: Task 4 의 `modify_order` 가 기록하는 `{"decision":"modified","notional":<signed delta>,"currency":…}`.
- Produces: `restore_spend` 가 `decision in ("placed","modified")` 를 통화별 합산하고 루프 종료 후 통화별 `max(0,…)` 하한. `preview_modify` 감사에 `currency` 포함.

- [ ] **Step 1: 실패 테스트** — `test_safety_tokens.py` 끝에 추가:

```python
def test_restore_spend_includes_modify_deltas():
    s = Settings(_env_file=None)
    m = SafetyManager(s, now=lambda: 1000.0, today=lambda: date(2026, 6, 17))
    events = [
        {"ts": "2026-06-17T01:00:00+00:00", "decision": "placed", "notional": "700000", "currency": "KRW"},
        {"ts": "2026-06-17T02:00:00+00:00", "decision": "modified", "notional": "10000", "currency": "KRW"},
    ]
    m.restore_spend(events)
    assert m._spent["KRW"] == Decimal("710000")  # placed + modify delta


def test_restore_spend_floors_negative_modify_deltas():
    s = Settings(_env_file=None)
    m = SafetyManager(s, now=lambda: 1000.0, today=lambda: date(2026, 6, 17))
    events = [
        {"ts": "2026-06-17T01:00:00+00:00", "decision": "placed", "notional": "100000", "currency": "KRW"},
        {"ts": "2026-06-17T02:00:00+00:00", "decision": "modified", "notional": "-300000", "currency": "KRW"},
    ]
    m.restore_spend(events)
    assert m._spent["KRW"] == Decimal("0")  # floored, not negative
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_safety_tokens.py::test_restore_spend_includes_modify_deltas" -v`
Expected: FAIL — `modified` 이벤트가 무시되어 `_spent==700000`.

- [ ] **Step 3: 구현 (a) restore_spend** — `safety.py::restore_spend` 에서 decision 필터와 루프 종료 하한을 수정. 현재 루프 시작:
```python
        for ev in events:
            if not isinstance(ev, dict) or ev.get("decision") != "placed":
                continue
```
를 교체:
```python
        for ev in events:
            if not isinstance(ev, dict) or ev.get("decision") not in ("placed", "modified"):
                continue
```
그리고 메서드 맨 끝(for 루프 종료 후)에 통화별 하한 추가:
```python
        for cur in self._spent:
            self._spent[cur] = max(Decimal("0"), self._spent[cur])
```

- [ ] **Step 4: 구현 (b) preview_modify 감사 currency** — `tools.py::preview_modify` 의 `app.audit.record({...})` 에 `"currency": spec.currency,` 추가(예: `"notional": spec.notional,` 다음 줄):
```python
        "symbol": symbol, "side": side, "notional": spec.notional, "currency": spec.currency,
```

- [ ] **Step 5: 통과 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_tokens.py -q`
Expected: PASS (신규 2건 포함). 기존 `test_restore_spend_sums_todays_placed_by_currency`·`test_restore_spend_skips_malformed_events_without_crashing` 무회귀(placed 만 있는 경우 동일, 하한은 양수에 무영향).

- [ ] **Step 6: 전체 무회귀 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q`
Expected: 전부 PASS.
Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q`
Expected: 전부 PASS (SDK 미변경 확인).

- [ ] **Step 7: Commit**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/tests/test_safety_tokens.py
git commit -m "feat(safety): persist modify deltas to audit and restore them on boot (floored)"
```

---

## Task 6: 문서 동기화 (CRITICAL RULE + 함정 + docs/claude + 테스트 수)

**Files:**
- Modify: `CLAUDE.md` (CRITICAL RULES modify 항목, 함정 절 C1/M1, Conventions 안전모델 한 줄)
- Modify: `docs/claude/tossinvest-mcp.md` (통화 판정·modify 토큰 생애·부팅 복원 절)
- Modify: 테스트 수 표기가 있는 곳(`CLAUDE.md` Commands, `pytossinvest/README.md`/`tossinvest-mcp/README.md` 해당 시)

**Interfaces:** (문서만 — 코드 인터페이스 변화 없음)

- [ ] **Step 1: 최종 테스트 수 확인**

```bash
uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q 2>&1 | tail -3
uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q 2>&1 | tail -3
```
두 명령의 통과 개수를 기록(MCP 신규 합계 = 기존 98 − release 1 + 신규 C1/M1 테스트; 정확한 수는 출력값 사용). 이 수치로 아래 문서 갱신.

- [ ] **Step 2: CLAUDE.md CRITICAL RULES — modify 불변식 수정** — `place_order` 안전 불변식 항목의 modify 문장을 교체. 현재:
> **modify 도 동형 2단계**(`preview_modify`→`modify_order(confirmation_token)`): consume → 가드레일 재검사(`check_daily=False`, M1 — 일일누적 미가산) → 실행 → 성공 시 `release`(pop only) / 실패 시 토큰 유지. 우회 금지.

를 교체:
> **modify 도 동형 2단계**(`preview_modify`→`modify_order(confirmation_token)`): consume → 가드레일 재검사(**델타 회계** — `check_daily=True, prev_notional=원본명목`, 일일 증분=`new−old`만 검사) → 실행 → 성공 시 `finalize(델타)`(pop + 부호있는 델타 가산, `record_spend` 0-하한) / 실패 시 토큰 유지. 우회 금지.

- [ ] **Step 3: CLAUDE.md 함정 절 — C1·M1 갱신** — 통화 판정 항목에서 `[C1 알려진 한계]` 문장을 "해결됨"으로 갱신:
> **통화 판정**(M1·C1 후속 반영): preview(`preview_order`/`preview_modify`)가 `get_prices([symbol])` 한 번으로 **권위 통화**(`Price.currency`)를 얻어 `build_spec(currency=…)` 로 주입; 조회 실패/빈결과/공백통화는 `order_currency(symbol)`(알파벳=USD·숫자=KRW) **폴백**. 즉 `BRK.B` 등도 API 통화가 있으면 정확, 없으면 종전 심볼모양으로 안전 강등. notional 단위는 주문통화, FX 환산 없음. KRW/USD 버킷 분리 유지.

그리고 `modify 일일누적 미가산(M1)` 항목을 교체:
> **modify 델타 회계(M1)** — modify 는 일일 버킷에 **부호있는 델타**(`new−old`)를 검사·가산. `preview_modify` 가 원본 주문 명목(`get_order` 의 price×qty)을 `spec.prev_notional` 로 잡고, 일일검사는 증분만(`spent+delta>cap` 이면 `daily-limit`), per-order/고액/하드실링은 여전히 전액. 성공 시 `finalize(델타)`, `record_spend` 가 0-하한. 한계: 일일버킷은 단순합이라 앱에서 직접 낸 주문을 다운사이즈하면 credit 되어 한도가 느슨해질 수 있음(0-하한으로 음수만 방지). 부팅복원은 `placed`+`modified` 델타 합산 후 0-하한.

- [ ] **Step 4: CLAUDE.md Conventions 안전모델 한 줄 갱신** — "preview→place / preview_modify→modify 2단계 + consume-on-success 멱등성(modify 는 `release`)" 에서 `modify 는 release` 를 `modify 는 finalize(델타)` 로 수정.

- [ ] **Step 5: docs/claude/tossinvest-mcp.md 동기화** — 통화 판정 절(`order_currency` 설명)·modify 토큰 생애 절·부팅 복원 절을 위 사실로 갱신(권위통화+폴백, 델타 회계+finalize, restore 가 modified 포함+0-하한, `release` 제거). `[C1 알려진 한계]` 문구 제거/갱신.

- [ ] **Step 6: 테스트 수 표기 갱신** — `CLAUDE.md` Commands 절의 `MCP (98)` 와 `pytossinvest/README.md`·`tossinvest-mcp/README.md` 의 테스트 수 표기를 Step 1 의 실제 수로 갱신(SDK 는 미변경이라 동일할 것).

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md docs/claude/tossinvest-mcp.md pytossinvest/README.md tossinvest-mcp/README.md
git commit -m "docs: sync C1 authoritative currency + M1 modify delta accounting"
```

---

## 완료 기준

- `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q` 전부 그린.
- `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q` 전부 그린(SDK 무회귀).
- C1: preview 가 API 권위 통화로 임계·버킷 선택, 실패 시 심볼모양 폴백, MARKET 단일 호출.
- M1: modify 가 델타를 일일 버킷에 검사+가산(0-하한), 재부팅 후 `modified` 델타까지 복원.
- `release` 제거(고아), 문서(CRITICAL RULE 포함) 동기화.

## 영향 파일 요약

| 파일 | Task |
|---|---|
| `tossinvest-mcp/src/tossinvest_mcp/safety.py` | 1(build_spec currency), 3(prev_notional+델타검사), 4(record_spend 하한·release 제거), 5(restore modified) |
| `tossinvest-mcp/src/tossinvest_mcp/tools.py` | 2(통화 조회·주입), 4(modify 델타 회계), 5(preview_modify 감사 currency) |
| `tossinvest-mcp/tests/test_safety_guardrails.py` | 1, 3 |
| `tossinvest-mcp/tests/test_tools_write.py` | 2, 4 |
| `tossinvest-mcp/tests/test_safety_tokens.py` | 4(release 테스트 제거·하한), 5(restore) |
| `CLAUDE.md`, `docs/claude/tossinvest-mcp.md`, READMEs | 6 |
