# 토스 Open API 안전 가드레일 강화 (feat/safety-hardening) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 적대적 감사로 찾은 10개 구멍을 닫는다 — 통화별 가드레일·2단계 modify·place 재검사·부팅 복원·양수검증·live 지연·SDK 응답/토큰 견고화 — 안전 불변식을 깨지 않고.

**Architecture:** `pytossinvest`(SDK) 와 `tossinvest-mcp`(MCP 안전레이어) 두 패키지. 변경은 거의 전부 MCP 안전레이어(`safety.py`·`tools.py`·`config.py`·`audit.py`·`server.py`)에 집중하고, SDK 는 응답 파싱·토큰 만료 견고화 2건만. 모든 변경은 기존 preview→place 토큰 패턴과 가드레일 순서를 보존한 채 *확장*한다.

**Tech Stack:** Python 3.12, uv 워크스페이스, `pytest`(+`respx` SDK / `FakeClient` MCP), `pydantic`/`pydantic-settings`, `httpx`. 돈/수량은 전구간 `Decimal`/문자열.

## Global Constraints

매 태스크의 요구사항에 아래가 암묵적으로 포함된다. 값은 spec/코드에서 verbatim.

- **돈/수량 float 금지** — 전구간 문자열/`Decimal`. `pytossinvest.money.to_decimal` 이 float 를 `TypeError` 로 거부(강제). 새 config 돈 필드도 `_no_float` validator 에 등록.
- **SDK 공개 API 깨지 말 것** — `tossinvest-mcp` 가 `pytossinvest` 에 의존. SDK 변경 후 **MCP 테스트도 그린** 확인.
- **`place_order` 안전 불변식** — 체결 경로(`paper.place`/`client.place_order`)는 **반드시 `safety.check_guardrails` 통과**. confirmation 토큰은 preview 계열(`preview_order`/신규 `preview_modify`)에서만 가드레일 통과 후 발급. 실행은 `consume(token)` → 실행 → **성공 시에만** `finalize`(place) / `release`(modify). 실패 시 토큰 유지(같은 `clientOrderId` 멱등 재시도).
- **가드레일 순서 불변** — `deny심볼 → allow심볼 → 하드실링(>) → 고액확인(>=) → 주문당상한 → 일일누적 → 장시간`. 재배열 금지.
- **상수 (verbatim)** — KRW: `HIGH_VALUE_THRESHOLD=100000000`(`>=`), `MAX_ORDER_THRESHOLD=3000000000`(`>`). USD: `HIGH_VALUE_THRESHOLD_USD=100000`, `MAX_ORDER_THRESHOLD_USD=3000000`. config 기본값 KRW `max_order_amount=1000000`/`daily_order_limit=5000000`, USD `max_order_amount_usd=1000`/`daily_order_limit_usd=5000`. `confirmation_ttl_sec=120`. `live_confirm_min_delay_sec=0`(기본 off, 권장 live+수동 `5`). `_EXPIRY_BUFFER_SEC=30.0`.
- **통화 판정 (FX 환산 X)** — `symbol.isalpha()` 면 `"USD"` 아니면 `"KRW"`. 기존 `_market_gate` 의 `"US"/"KR"` 휴리스틱과 동형. 1억/30억 KRW 임계는 KRW 가정.
- **M1 modify 회계** — modify 는 정정 후 notional 에 대해 주문당상한·고액확인·하드실링·allow/deny 를 검사하되 **일일누적 버킷에는 가산/검사하지 않는다**(`check_daily=False`). 델타 회계 없음.
- **건드리지 말 것 (회귀 금지)** — 토큰 위조불가(uuid4·서버발급·spec바인딩·1회용·TTL), 모드 구조적 게이트(read_only=툴 미등록), live 이중게이트(`mode=live`+`ALLOW_LIVE=1`), `Decimal` 규율, paper 샌드박스 격리.
- **커밋 정책 (사용자 확정)**: 각 태스크 마지막 스텝에서 **`feat/safety-hardening` 브랜치에 실제 커밋**(TDD 빈번커밋 + 체크포인트). 따라서 각 태스크의 "스테이징 + 메시지 준비" 스텝은 `git add` 후 그 줄의 메시지로 **`git commit` 까지 수행**한다. **push/merge 는 사용자 별도 요청 시에만.** main 직접 커밋 금지.
- **AI 작성 표시 금지** — 커밋 메시지·코드 주석·문서 어디에도 `Co-Authored-By: Claude` / "Generated with AI" 등 금지.
- **기존 테스트 그린 유지** (MCP 64 + SDK 42). 설계가 내부표현을 바꾸는 아래 3건만 예외(각 해당 태스크에서 갱신):
  - `test_safety_tokens.py::test_finalize_consumes_token_and_records_spend` — `_spent` 가 통화별 dict 가 되므로 `m._spent == Decimal("700000")` → `m._spent["KRW"] == Decimal("700000")` (Task 5).
  - `test_tools_write.py::test_modify_and_cancel_are_live_only` — modify 가 2단계가 되므로 재작성 (Task 9, 사용자 승인된 예외).
  - `test_server_modes.py` — `WRITE_TOOLS` 에 `preview_modify` 추가, 13→14 툴 (Task 9).

**브랜치:** `feat/safety-hardening`.

**테스트 명령:**
- SDK: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests`
- MCP: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests`

---

## File Structure

변경/생성 파일과 책임:

- `pytossinvest/src/pytossinvest/auth.py` — 토큰 만료시각 음수 클램프 (#9).
- `pytossinvest/src/pytossinvest/client.py` — `_request` 200 경로 JSON/`result` 견고화 (#7,#8).
- `pytossinvest/tests/test_auth.py`, `pytossinvest/tests/test_client_core.py` — 위 회귀 테스트.
- `tossinvest-mcp/src/tossinvest_mcp/safety.py` — 핵심. 양수검증(#6)·통화필드/`order_currency`(#3)·통화별 `check_guardrails`+`_spent` dict+`record_spend`/`finalize`+`check_daily`(#3)·`release`+min-delay(#4)·`restore_spend`(#5)·`modify_order_id`(#1).
- `tossinvest-mcp/src/tossinvest_mcp/config.py` — USD 상한 2필드(#3) + `live_confirm_min_delay_sec`(#4).
- `tossinvest-mcp/src/tossinvest_mcp/audit.py` — `read_events()` JSONL 파서(#5).
- `tossinvest-mcp/src/tossinvest_mcp/tools.py` — `place_order` 재검사(#2)·place 감사 통화/notional(#5)·`preview_modify`+2단계 `modify_order`(#1)·`cancel_order` previousStatus(#10).
- `tossinvest-mcp/src/tossinvest_mcp/server.py` — 부팅 `restore_spend` 와이어링(#5)·`preview_modify` 등록 + `modify_order` 시그니처 교체(#1).
- `tossinvest-mcp/tests/conftest.py` — `FakeClient.get_order` 를 현실적 주문으로 보강(modify/cancel 용).
- `tossinvest-mcp/tests/*` — 신규/갱신 테스트.
- `tossinvest-mcp/README.md`, `CLAUDE.md`, `docs/claude/tossinvest-mcp.md` — 문서.

---

## Task 0: 브랜치 + 베이스라인 그린

**Files:** (없음 — git 만)

- [ ] **Step 1: feat 브랜치 생성**

```bash
git checkout -b feat/safety-hardening
```

- [ ] **Step 2: 베이스라인 그린 확인 (수정 전 진실)**

Run:
```bash
uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q
uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q
```
Expected: SDK `42 passed`, MCP `64 passed`. (하나라도 빨가면 멈추고 보고 — 환경 문제 먼저 해결.)

---

## Task 1: SDK — 토큰 만료시각 음수 클램프 (#9)

`expires_in` 이 버퍼(30s)보다 작으면 `expires_at` 이 발급시각보다 과거가 되어 매 호출 재발급. `max(0.0, …)` 로 클램프.

**Files:**
- Modify: `pytossinvest/src/pytossinvest/auth.py:58`
- Test: `pytossinvest/tests/test_auth.py`

**Interfaces:**
- Consumes: `TokenManager(client_id, client_secret, *, http, now)`, `_EXPIRY_BUFFER_SEC=30.0`, 내부 `_expires_at: float`.
- Produces: 동작 변경만 — `_expires_at = now + max(0.0, expires_in - 30.0)` (음수 불가).

- [ ] **Step 1: 실패 테스트 작성** — `pytossinvest/tests/test_auth.py` 끝에 추가:

```python
@respx.mock
def test_short_expiry_is_clamped_not_in_past():
    clock = FakeClock()  # t = 1000.0
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "token_type": "Bearer", "expires_in": 10})  # 10 < buffer(30)
    )
    mgr, _ = _mgr(clock)
    mgr.get_token()
    # without clamp: 1000 + 10 - 30 = 980 (< issue time). with clamp: 1000 + max(0,-20) = 1000.
    assert mgr._expires_at >= 1000.0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests/test_auth.py::test_short_expiry_is_clamped_not_in_past -v`
Expected: FAIL — `assert 980.0 >= 1000.0`.

- [ ] **Step 3: 구현** — `auth.py:58` 한 줄 교체:

```python
        self._expires_at = self._now() + max(0.0, float(body["expires_in"]) - _EXPIRY_BUFFER_SEC)
```

(교체 대상 기존 줄: `self._expires_at = self._now() + float(body["expires_in"]) - _EXPIRY_BUFFER_SEC`)

- [ ] **Step 4: 통과 확인 (해당 + 전체 auth)**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests/test_auth.py -v`
Expected: 기존 5 + 신규 1 = PASS.

- [ ] **Step 5: 변경 스테이징 + 메시지 준비 (커밋은 체크포인트에서 승인 후)**

```bash
git add pytossinvest/src/pytossinvest/auth.py pytossinvest/tests/test_auth.py
# 메시지: "fix(auth): clamp token expiry so short-lived tokens aren't issued already-expired"
```

---

## Task 2: SDK — `_request` 200 경로 JSON/`result` 견고화 (#7, #8)

200 응답이 비정상(비 JSON, `result` 키 부재)일 때 조용히 `None` 반환하던 걸 타입드 에러로 거부.

**Files:**
- Modify: `pytossinvest/src/pytossinvest/client.py:11` (import), `:100-101` (200 분기)
- Test: `pytossinvest/tests/test_client_core.py`

**Interfaces:**
- Consumes: `error_from_response`, 신규 `TossInvestError`(from `.errors`), `self._request(method, path, *, group, ...)`.
- Produces: 200 경로가 (a) 비 JSON → `TossInvestError("invalid-response", http_status=200)`, (b) `result` 키 부재 → `TossInvestError("missing-result", http_status=200)`, 정상 → `body["result"]`.

- [ ] **Step 1: 실패 테스트 작성** — `pytossinvest/tests/test_client_core.py` 끝에 추가:

```python
@respx.mock
def test_200_non_json_body_raises_invalid_response():
    from pytossinvest.errors import TossInvestError
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(200, text="not json")
    )
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert exc.value.code == "invalid-response"


@respx.mock
def test_200_missing_result_raises_missing_result():
    from pytossinvest.errors import TossInvestError
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(200, json={"data": [1, 2, 3]})  # no "result" key
    )
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert exc.value.code == "missing-result"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests/test_client_core.py::test_200_non_json_body_raises_invalid_response pytossinvest/tests/test_client_core.py::test_200_missing_result_raises_missing_result -v`
Expected: FAIL — 현재 200 경로는 `resp.json()`(JSONDecodeError 비포장) / `.get("result")`(None 반환)이라 우리가 기대한 `TossInvestError(code=...)` 안 나옴.

- [ ] **Step 3: 구현 (a) import 확장** — `client.py:11` 교체:

```python
from .errors import error_from_response, TossInvestError
```

- [ ] **Step 4: 구현 (b) 200 분기 교체** — `client.py` 의 `if resp.status_code == 200:\n    return resp.json().get("result")` 를 아래로 교체:

```python
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                raise TossInvestError(
                    "invalid-response", "200 response body was not valid JSON",
                    http_status=200,
                )
            if "result" not in body:
                raise TossInvestError(
                    "missing-result", "200 response had no 'result' field",
                    http_status=200,
                )
            return body["result"]
```

- [ ] **Step 5: 통과 확인 (전체 SDK — 모든 엔드포인트 테스트가 `{"result": …}` 사용하므로 회귀 없음)**

Run: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q`
Expected: 42 + 2 = `44 passed`.

- [ ] **Step 6: 스테이징 + 메시지 준비**

```bash
git add pytossinvest/src/pytossinvest/client.py pytossinvest/tests/test_client_core.py
# 메시지: "fix(client): reject malformed 200 responses (non-JSON / missing result) instead of returning None"
```

---

## Task 3: MCP — `build_spec` 비양수 주문값 거부 (#6)

`quantity`/`price`/`order_amount` 가 0 이하면 notional 계산 전에 `invalid-order-value` 로 거부(음수/0 주문 차단).

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`build_spec` 본문 선두)
- Test: `tossinvest-mcp/tests/test_safety_guardrails.py`

**Interfaces:**
- Consumes: `to_decimal`, `GuardrailError(code, message)`, `SafetyManager.build_spec(...)`.
- Produces: `build_spec` 가 0 이하 `quantity`/`price`/`order_amount` 에 대해 `GuardrailError("invalid-order-value")`. 정상 양수 입력은 기존과 동일.

- [ ] **Step 1: 실패 테스트 작성** — `tossinvest-mcp/tests/test_safety_guardrails.py` 끝에 추가:

```python
def test_build_spec_rejects_nonpositive_quantity():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="0", price="70000")
    assert e.value.code == "invalid-order-value"


def test_build_spec_rejects_negative_price():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="10", price="-1")
    assert e.value.code == "invalid-order-value"


def test_build_spec_rejects_nonpositive_order_amount():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET", order_amount="0")
    assert e.value.code == "invalid-order-value"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_safety_guardrails.py::test_build_spec_rejects_nonpositive_quantity" "tossinvest-mcp/tests/test_safety_guardrails.py::test_build_spec_rejects_negative_price" "tossinvest-mcp/tests/test_safety_guardrails.py::test_build_spec_rejects_nonpositive_order_amount" -v`
Expected: FAIL — 현재 `quantity="0"` 은 notional 0 으로 통과(GuardrailError 안 남); `price="-1"` 은 음수 notional.

- [ ] **Step 3: 구현** — `safety.py` 의 `build_spec` 첫 줄(`if order_amount is not None:`) **앞에** 검증 루프 삽입:

```python
        for label, val in (("quantity", quantity), ("price", price), ("order_amount", order_amount)):
            if val is not None and to_decimal(val) <= 0:
                raise GuardrailError(
                    "invalid-order-value", f"{label} must be a positive number, got {val!r}"
                )
```

- [ ] **Step 4: 통과 확인 (신규 + 기존 build_spec 테스트 — 전부 양수라 회귀 없음)**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py -v`
Expected: 기존 + 신규 3 = PASS.

- [ ] **Step 5: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/tests/test_safety_guardrails.py
# 메시지: "feat(safety): reject non-positive order values in build_spec"
```

---

## Task 4: MCP — 통화 인식 토대 (`OrderSpec.currency`, `order_currency`, USD config) (#3 part 1)

`OrderSpec` 에 `currency` + `modify_order_id` 추가, `order_currency(symbol)` 헬퍼, config 에 USD 상한 2필드. (가드레일 임계 선택은 Task 5.)

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`OrderSpec` 데이터클래스, 모듈 함수 `order_currency`, `build_spec` 반환부 + 시그니처)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/config.py` (USD 필드 + `_no_float` 등록)
- Test: `tossinvest-mcp/tests/test_safety_guardrails.py`, `tossinvest-mcp/tests/test_config.py`

**Interfaces:**
- Consumes: Task 3 의 `build_spec`(양수검증 포함).
- Produces:
  - 모듈 함수 `order_currency(symbol: str) -> str` ("USD" if `symbol.isalpha()` else "KRW").
  - `OrderSpec` 필드 추가: `currency: str`, `modify_order_id: "str | None" = None`.
  - `build_spec(..., modify_order_id: "str | None" = None)` — `currency=order_currency(symbol)`, `modify_order_id` 채움.
  - `Settings.max_order_amount_usd: Decimal = Decimal("1000")`, `Settings.daily_order_limit_usd: Decimal = Decimal("5000")` (둘 다 `_no_float`).

- [ ] **Step 1: 실패 테스트 작성** — `test_safety_guardrails.py` 끝에:

```python
from tossinvest_mcp.safety import order_currency


def test_order_currency_alpha_is_usd_numeric_is_krw():
    assert order_currency("AAPL") == "USD"
    assert order_currency("005930") == "KRW"


def test_build_spec_sets_currency_and_modify_id():
    m = _mgr()
    krw = m.build_spec(symbol="005930", side="BUY", order_type="LIMIT", quantity="1", price="70000")
    usd = m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET", order_amount="100",
                       modify_order_id="ord-9")
    assert krw.currency == "KRW" and krw.modify_order_id is None
    assert usd.currency == "USD" and usd.modify_order_id == "ord-9"
```

그리고 `test_config.py` 끝에:

```python
def test_usd_caps_default_and_decimal():
    s = _settings()
    assert s.max_order_amount_usd == Decimal("1000")
    assert s.daily_order_limit_usd == Decimal("5000")
    assert isinstance(s.max_order_amount_usd, Decimal)


def test_usd_caps_reject_float():
    with pytest.raises(Exception):
        _settings(max_order_amount_usd=1000.5)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_safety_guardrails.py::test_order_currency_alpha_is_usd_numeric_is_krw" "tossinvest-mcp/tests/test_config.py::test_usd_caps_default_and_decimal" -v`
Expected: FAIL — `order_currency` ImportError / `max_order_amount_usd` 속성 없음.

- [ ] **Step 3: 구현 (a) `safety.py` — `order_currency` 모듈 함수 추가** (상수 `MAX_ORDER_THRESHOLD` 정의 줄 **뒤**, `class GuardrailError` **앞**):

```python
def order_currency(symbol: str) -> str:
    """Order currency by symbol shape: alphabetic = USD, numeric = KRW (no FX)."""
    return "USD" if symbol.isalpha() else "KRW"
```

- [ ] **Step 4: 구현 (b) `safety.py` — `OrderSpec` 에 필드 2개 추가** (`client_order_id: str` 뒤):

```python
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
    currency: str
    modify_order_id: "str | None" = None
```

- [ ] **Step 5: 구현 (c) `safety.py` — `build_spec` 시그니처 + 반환부** (Task 3 의 양수검증 루프 유지). `build_spec` 전체를 아래로 교체:

```python
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
        modify_order_id: "str | None" = None,
    ) -> OrderSpec:
        for label, val in (("quantity", quantity), ("price", price), ("order_amount", order_amount)):
            if val is not None and to_decimal(val) <= 0:
                raise GuardrailError(
                    "invalid-order-value", f"{label} must be a positive number, got {val!r}"
                )
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
            client_order_id=self._gen_id(), currency=order_currency(symbol),
            modify_order_id=modify_order_id,
        )
```

- [ ] **Step 6: 구현 (d) `config.py` — USD 필드 + validator 등록**. `max_order_amount`/`daily_order_limit` 정의 **바로 뒤**에 추가:

```python
    max_order_amount_usd: Decimal = Decimal("1000")
    daily_order_limit_usd: Decimal = Decimal("5000")
```

그리고 `_no_float` validator 의 필드 목록을 교체:

```python
    @field_validator(
        "max_order_amount", "daily_order_limit", "paper_starting_cash",
        "max_order_amount_usd", "daily_order_limit_usd", mode="before",
    )
```

- [ ] **Step 7: 통과 확인 (safety + config 전체 — 기존 그린 유지)**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py tossinvest-mcp/tests/test_config.py tossinvest-mcp/tests/test_safety_tokens.py -v`
Expected: 전부 PASS (기존 `build_spec`/토큰 테스트는 `currency` 자동세팅·신규 필드 기본값으로 무회귀).

- [ ] **Step 8: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/src/tossinvest_mcp/config.py tossinvest-mcp/tests/test_safety_guardrails.py tossinvest-mcp/tests/test_config.py
# 메시지: "feat(safety): currency-aware OrderSpec + order_currency helper + USD config caps"
```

---

## Task 5: MCP — 통화별 가드레일 + 통화별 일일누적 + `check_daily` (#3 part 2)

`check_guardrails` 가 `spec.currency` 로 네 게이트(주문당·일일·고액·하드실링) 임계 세트를 선택. `_spent` 를 통화별 dict 로, `record_spend(notional, currency="KRW")`, `finalize` 가 소비된 spec 의 통화로 기록. `check_daily=False` 면 일일 게이트 스킵(modify M1·place 재검사 옵션용).

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`HIGH_VALUE_THRESHOLD_USD`/`MAX_ORDER_THRESHOLD_USD` 상수, `SafetyManager.__init__` 의 `_spent`, `check_guardrails`, `_roll_daily`, `record_spend`, `finalize`)
- Test: `tossinvest-mcp/tests/test_safety_guardrails.py`, `tossinvest-mcp/tests/test_safety_tokens.py` (1줄 갱신)

**Interfaces:**
- Consumes: Task 4 의 `spec.currency`, `order_currency`, config `max_order_amount_usd`/`daily_order_limit_usd`.
- Produces:
  - 상수 `HIGH_VALUE_THRESHOLD_USD = Decimal("100000")`, `MAX_ORDER_THRESHOLD_USD = Decimal("3000000")`.
  - `check_guardrails(spec, *, is_market_open, enforce_hours, check_daily: bool = True)` — 통화별 임계; `check_daily=False` 면 일일 게이트만 스킵(순서·다른 게이트 불변).
  - `self._spent: dict[str, Decimal]` (`{"KRW":0,"USD":0}`).
  - `record_spend(notional, currency: str = "KRW")`, `finalize(token, notional)`(소비 spec 통화 유도).

- [ ] **Step 1: 실패 테스트 작성** — `test_safety_guardrails.py` 끝에:

```python
def _usd_spec(m, **kw):
    base = dict(symbol="AAPL", side="BUY", order_type="LIMIT", quantity="1", price="100")
    base.update(kw)
    return m.build_spec(**base)


def test_usd_per_order_cap_uses_usd_threshold():
    m = _mgr(max_order_amount="1000000", max_order_amount_usd="1000")
    spec = _usd_spec(m, quantity="20", price="100")  # $2,000 > $1,000 cap (KRW cap irrelevant)
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "order-amount-cap"


def test_usd_high_value_threshold_is_100k_usd():
    m = _mgr(max_order_amount_usd="999999999", daily_order_limit_usd="999999999")
    spec = _usd_spec(m, quantity="2000", price="100")  # $200,000 >= $100,000
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "confirm-high-value-required"


def test_usd_hard_ceiling_is_3m_usd():
    m = _mgr(max_order_amount_usd="999999999", daily_order_limit_usd="999999999")
    spec = _usd_spec(m, quantity="40000", price="100", confirm_high_value_order=True)  # $4,000,000 > $3,000,000
    with pytest.raises(GuardrailError) as e:
        _ok(m, spec)
    assert e.value.code == "max-order-exceeded"


def test_daily_buckets_are_per_currency():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000",
             max_order_amount_usd="9000", daily_order_limit_usd="9000")
    krw = _spec(m, quantity="10", price="70000")  # 700,000 KRW
    m.check_guardrails(krw, is_market_open=True, enforce_hours=False)
    m.record_spend(krw.notional, krw.currency)
    # a USD order is unaffected by the KRW bucket being near its limit
    usd = _usd_spec(m, quantity="1", price="100")  # $100
    m.check_guardrails(usd, is_market_open=True, enforce_hours=False)  # must NOT raise
    # but a second KRW order tips the KRW bucket over
    krw2 = _spec(m, quantity="10", price="70000")
    with pytest.raises(GuardrailError) as e:
        m.check_guardrails(krw2, is_market_open=True, enforce_hours=False)
    assert e.value.code == "daily-limit"


def test_check_daily_false_skips_daily_gate():
    m = _mgr(max_order_amount="9000000", daily_order_limit="1000000")
    m.record_spend(Decimal("900000"), "KRW")
    spec = _spec(m, quantity="10", price="70000")  # +700,000 -> over 1,000,000
    # default would raise daily-limit; check_daily=False skips it (other gates still run)
    m.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=False)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py -k "usd or per_currency or check_daily" -v`
Expected: FAIL — USD 임계 미적용(현재 전부 KRW 상수), `check_daily` 인자 없음 → `TypeError`.

- [ ] **Step 3: 구현 (a) USD 상수 추가** — `safety.py` 의 `MAX_ORDER_THRESHOLD` 정의 줄 뒤:

```python
HIGH_VALUE_THRESHOLD_USD = Decimal("100000")   # $100k: requires explicit confirm
MAX_ORDER_THRESHOLD_USD = Decimal("3000000")   # $3M: always rejected
```

- [ ] **Step 4: 구현 (b) `__init__` 의 `_spent` 를 dict 로** — `safety.py` 의 `self._spent: Decimal = Decimal("0")` 교체:

```python
        self._spent: dict[str, Decimal] = {"KRW": Decimal("0"), "USD": Decimal("0")}
```

- [ ] **Step 5: 구현 (c) `check_guardrails` 통화별 + `check_daily`** — 메서드 전체 교체:

```python
    def check_guardrails(
        self, spec: OrderSpec, *, is_market_open: bool, enforce_hours: bool,
        check_daily: bool = True,
    ) -> None:
        cfg = self._cfg
        if spec.currency == "USD":
            high_value = HIGH_VALUE_THRESHOLD_USD
            hard_ceiling = MAX_ORDER_THRESHOLD_USD
            per_order_cap = to_decimal(cfg.max_order_amount_usd)
            daily_cap = to_decimal(cfg.daily_order_limit_usd)
        else:
            high_value = HIGH_VALUE_THRESHOLD
            hard_ceiling = MAX_ORDER_THRESHOLD
            per_order_cap = to_decimal(cfg.max_order_amount)
            daily_cap = to_decimal(cfg.daily_order_limit)
        if cfg.deny_symbols and spec.symbol in cfg.deny_symbols:
            raise GuardrailError("symbol-denied", f"{spec.symbol} is in the deny list")
        if cfg.allow_symbols and spec.symbol not in cfg.allow_symbols:
            raise GuardrailError("symbol-not-allowed", f"{spec.symbol} is not in the allow list")
        if spec.notional > hard_ceiling:
            raise GuardrailError(
                "max-order-exceeded",
                f"notional {spec.notional} {spec.currency} exceeds the hard {hard_ceiling} ceiling",
            )
        if spec.notional >= high_value and not spec.confirm_high_value_order:
            raise GuardrailError(
                "confirm-high-value-required",
                f"orders >= {high_value} {spec.currency} require confirm_high_value_order=true",
            )
        if spec.notional > per_order_cap:
            raise GuardrailError(
                "order-amount-cap",
                f"notional {spec.notional} {spec.currency} exceeds per-order cap {per_order_cap}",
            )
        if check_daily:
            self._roll_daily()
            if self._spent[spec.currency] + spec.notional > daily_cap:
                raise GuardrailError(
                    "daily-limit",
                    f"this order would push today's {spec.currency} total over {daily_cap}",
                )
        if enforce_hours and not is_market_open:
            raise GuardrailError(
                "market-closed",
                "market is closed (set enforce_market_hours=false to override)",
            )
```

- [ ] **Step 6: 구현 (d) `_roll_daily` 둘 다 리셋** — 교체:

```python
    def _roll_daily(self) -> None:
        d = self._today()
        if self._spent_date != d:
            self._spent_date = d
            self._spent = {"KRW": Decimal("0"), "USD": Decimal("0")}
```

- [ ] **Step 7: 구현 (e) `record_spend` + `finalize`** — 둘 다 교체:

```python
    def record_spend(self, notional: Decimal, currency: str = "KRW") -> None:
        self._roll_daily()
        self._spent[currency] = self._spent.get(currency, Decimal("0")) + notional
```

```python
    def finalize(self, token: str, notional: Decimal) -> None:
        pending = self._pending.pop(token, None)
        currency = pending.spec.currency if pending else "KRW"
        self.record_spend(notional, currency)
```

- [ ] **Step 8: 기존 토큰 테스트 1줄 갱신** — `test_safety_tokens.py::test_finalize_consumes_token_and_records_spend` 의 마지막 단언 교체:

```python
    assert m._spent["KRW"] == Decimal("700000")
```

(교체 대상: `assert m._spent == Decimal("700000")`. `_spent` 가 통화별 dict 가 된 직접 결과 — 동작 동일, 내부표현만 갱신.)

- [ ] **Step 9: 통과 확인 (safety + tokens 전체)**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py tossinvest-mcp/tests/test_safety_tokens.py -v`
Expected: 기존(갱신 1) + 신규 5 = PASS. (기존 KRW 가드레일·`test_daily_limit_accumulates`(`record_spend` 기본 KRW) 무회귀.)

- [ ] **Step 10: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/tests/test_safety_guardrails.py tossinvest-mcp/tests/test_safety_tokens.py
# 메시지: "feat(safety): per-currency guardrail thresholds, per-currency daily buckets, check_daily flag"
```

---

## Task 6: MCP — `place_order` consume 직후 일일한도 재검사 (#2)

preview 들이 각각 한도 아래여도, 다른 주문이 먼저 체결되면 합산이 한도를 넘을 수 있다. consume 직후·실행 전에 갱신된 `_spent` 로 금액 게이트만 재실행(장운영 재조회 스킵). 거부 시 토큰 미finalize → 멱등 보존.

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/tools.py` (`place_order` — `consume` 다음 줄)
- Test: `tossinvest-mcp/tests/test_tools_write.py`

**Interfaces:**
- Consumes: Task 5 의 `check_guardrails(spec, *, is_market_open, enforce_hours, check_daily=True)`, 통화별 `_spent`.
- Produces: `place_order` 가 `consume` 직후 `app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False)` 호출(기본 `check_daily=True`). 거부 시 토큰은 `_pending` 에 남음.

- [ ] **Step 1: 실패 테스트 작성** — `test_tools_write.py` 끝에:

```python
def test_place_rechecks_daily_limit_after_other_fill(app_factory):
    app = app_factory(mode="paper", daily_order_limit="1000000", max_order_amount="1000000")
    pv1 = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                          quantity="10", price="70000")  # 700,000 (under limit individually)
    pv2 = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                          quantity="10", price="70000")  # 700,000 (also under, at preview time)
    T.place_order(app, confirmation_token=pv1["confirmationToken"])  # records 700,000
    with pytest.raises(GuardrailError) as e:
        T.place_order(app, confirmation_token=pv2["confirmationToken"])  # 1,400,000 > 1,000,000
    assert e.value.code == "daily-limit"
    # token NOT consumed -> still pending (idempotency preserved)
    assert app.safety.consume(pv2["confirmationToken"]).client_order_id == pv2["clientOrderId"]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_tools_write.py::test_place_rechecks_daily_limit_after_other_fill" -v`
Expected: FAIL — 현재 place 는 재검사 없이 pv2 를 체결(두 번째 주문이 한도 초과인데도 통과).

- [ ] **Step 3: 구현** — `tools.py` 의 `place_order` 에서 `spec = app.safety.consume(confirmation_token)  # validates exists + not expired` 줄 **바로 뒤**에 삽입:

```python
    # re-check amount guardrails against the now-updated daily spend (idempotency-safe:
    # rejection happens before any execution, so the token is not finalized)
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False)
```

- [ ] **Step 4: 통과 확인 (write 전체 — 단일주문 기존 테스트는 `_spent=0` 이라 재검사 통과)**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_write.py -v`
Expected: 신규 1 + 기존(단, `test_modify_and_cancel_are_live_only` 은 Task 9 에서 재작성 — 여기선 아직 기존대로 그린) = PASS.

- [ ] **Step 5: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/tests/test_tools_write.py
# 메시지: "feat(tools): re-check daily limit at place time using updated spend (idempotency-safe)"
```

---

## Task 7: MCP — `release()` + live confirm 최소지연 (#4 메커니즘)

`release(token)`(pop only, 일일누적 미가산 — modify 용). live 최소지연: `_Pending.issued_at` 저장, `consume` 은 `cfg.is_live` 이고 `live_confirm_min_delay_sec>0` 일 때만 강제. 기본 0 → off(64 테스트 + 멱등 재시도 그린 유지).

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/config.py` (`live_confirm_min_delay_sec`)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`_Pending.issued_at`, `issue_token`, `consume`, `release`)
- Test: `tossinvest-mcp/tests/test_safety_tokens.py`, `tossinvest-mcp/tests/test_config.py`

**Interfaces:**
- Consumes: `self._cfg.is_live`, `self._now`, `_Pending`.
- Produces:
  - `Settings.live_confirm_min_delay_sec: int = 0`.
  - `_Pending(spec, expires_at, issued_at)`.
  - `consume(token)` 가 live+지연>0 시 `now-issued_at < delay` 면 `GuardrailError("confirm-too-soon")`.
  - `SafetyManager.release(token)` — `_pending.pop(token, None)` (record_spend 안 함).

- [ ] **Step 1: 실패 테스트 작성** — `test_safety_tokens.py` 끝에:

```python
def _live_mgr(clock, **overrides):
    s = Settings(_env_file=None, mode="live", allow_live=True,
                 confirmation_ttl_sec=120, **overrides)
    return SafetyManager(s, now=clock, today=lambda: date(2026, 6, 17))


def test_live_min_delay_blocks_immediate_consume_then_allows():
    clock = Clock()
    m = _live_mgr(clock, live_confirm_min_delay_sec=5)
    token = m.issue_token(_spec(m))
    with pytest.raises(GuardrailError) as e:
        m.consume(token)  # 0s since issue, < 5
    assert e.value.code == "confirm-too-soon"
    clock.advance(5)
    assert m.consume(token).client_order_id  # now allowed


def test_min_delay_off_by_default_even_in_live():
    clock = Clock()
    m = _live_mgr(clock)  # live_confirm_min_delay_sec defaults 0
    token = m.issue_token(_spec(m))
    assert m.consume(token).client_order_id  # immediate consume OK


def test_release_pops_without_recording_spend():
    clock = Clock()
    m = _mgr(clock, daily_order_limit="999999999")
    spec = _spec(m)
    token = m.issue_token(spec)
    m.release(token)
    assert m._spent["KRW"] == Decimal("0")  # NOT recorded
    with pytest.raises(GuardrailError) as e:
        m.consume(token)  # token gone
    assert e.value.code == "invalid-confirmation"
```

그리고 `test_config.py` 끝에:

```python
def test_live_confirm_min_delay_default_zero():
    assert _settings().live_confirm_min_delay_sec == 0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_tokens.py -k "min_delay or release" tossinvest-mcp/tests/test_config.py::test_live_confirm_min_delay_default_zero -v`
Expected: FAIL — `live_confirm_min_delay_sec` 없음, `release` 없음, `_Pending` issued_at 없음.

- [ ] **Step 3: 구현 (a) config 필드** — `config.py` 의 `confirmation_ttl_sec: int = 120` 줄 뒤:

```python
    # live-only: minimum seconds between preview and place (0 = off). 권장 live+수동 5.
    live_confirm_min_delay_sec: int = 0
```

- [ ] **Step 4: 구현 (b) `_Pending` 에 `issued_at`** — `safety.py` 의 `_Pending` 교체:

```python
@dataclass
class _Pending:
    spec: OrderSpec
    expires_at: float
    issued_at: float
```

- [ ] **Step 5: 구현 (c) `issue_token` 가 issued_at 기록** — 교체:

```python
    def issue_token(self, spec: OrderSpec) -> str:
        token = self._gen_id()
        now = self._now()
        self._pending[token] = _Pending(
            spec=spec, expires_at=now + self._cfg.confirmation_ttl_sec, issued_at=now
        )
        return token
```

- [ ] **Step 6: 구현 (d) `consume` 에 최소지연 게이트** — 교체:

```python
    def consume(self, token: str) -> OrderSpec:
        pending = self._pending.get(token)
        if pending is None:
            raise GuardrailError(
                "invalid-confirmation",
                "unknown or already-used confirmation_token; run preview again",
            )
        if self._now() > pending.expires_at:
            del self._pending[token]
            raise GuardrailError(
                "expired-confirmation",
                "confirmation_token expired; run preview again",
            )
        delay = self._cfg.live_confirm_min_delay_sec
        if self._cfg.is_live and delay > 0 and self._now() - pending.issued_at < delay:
            raise GuardrailError(
                "confirm-too-soon",
                f"live order must wait {delay}s after preview before placing",
            )
        return pending.spec
```

- [ ] **Step 7: 구현 (e) `release` 추가** — `finalize` 메서드 **뒤**에:

```python
    def release(self, token: str) -> None:
        """Drop a pending token without recording spend (modify: per-order gated, no daily bucket)."""
        self._pending.pop(token, None)
```

- [ ] **Step 8: 통과 확인 (tokens + config 전체)**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_tokens.py tossinvest-mcp/tests/test_config.py -v`
Expected: 기존(default 0 이라 무회귀) + 신규 4 = PASS.

- [ ] **Step 9: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/src/tossinvest_mcp/config.py tossinvest-mcp/tests/test_safety_tokens.py tossinvest-mcp/tests/test_config.py
# 메시지: "feat(safety): add token release() and opt-in live confirm min-delay gate"
```

---

## Task 8: MCP — 부팅 시 `_spent` 복원 (#5)

place 감사에 `currency`+`notional` 추가, `AuditLog.read_events()` JSONL 파서, `SafetyManager.restore_spend(events)` 가 오늘자(UTC `ts`→KST 날짜) `decision=="placed"` notional 을 통화별 버킷 합산, `server.build_app_context` 부팅에 연결.

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/tools.py` (`place_order` placed 감사 레코드)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/audit.py` (`read_events`)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`restore_spend` + KST import)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/server.py` (`build_app_context` 와이어링)
- Test: `tossinvest-mcp/tests/test_audit.py`, `tossinvest-mcp/tests/test_safety_tokens.py`, `tossinvest-mcp/tests/test_tools_write.py`, `tossinvest-mcp/tests/test_server_modes.py`

**Interfaces:**
- Consumes: Task 5 의 통화별 `_spent`/`record_spend`, `self._today()`(KST date).
- Produces:
  - place 감사 레코드에 `"currency": spec.currency, "notional": spec.notional` 추가.
  - `AuditLog.read_events() -> list[dict]` (파일 없으면 `[]`, 깨진 줄 스킵).
  - `SafetyManager.restore_spend(events: list[dict]) -> None` — 오늘자 placed 합산.
  - `build_app_context` 가 `safety.restore_spend(audit.read_events())` 호출.

- [ ] **Step 1: 실패 테스트 작성 (a) audit** — `test_audit.py` 끝에:

```python
def test_read_events_parses_and_skips_blank(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, now=_fixed_clock)
    log.record({"tool": "place_order", "decision": "placed",
                "notional": Decimal("70000"), "currency": "KRW"})
    log.record({"tool": "preview_order", "decision": "previewed"})
    events = log.read_events()
    assert [e["decision"] for e in events] == ["placed", "previewed"]
    assert events[0]["notional"] == "70000"  # serialized as string


def test_read_events_missing_file_is_empty(tmp_path):
    assert AuditLog(tmp_path / "nope.log").read_events() == []
```

**Step 1 (b) restore** — `test_safety_tokens.py` 끝에:

```python
def test_restore_spend_sums_todays_placed_by_currency():
    s = Settings(_env_file=None)
    m = SafetyManager(s, now=lambda: 1000.0, today=lambda: date(2026, 6, 17))
    events = [
        {"ts": "2026-06-17T01:00:00+00:00", "decision": "placed", "notional": "700000", "currency": "KRW"},
        {"ts": "2026-06-16T20:00:00+00:00", "decision": "placed", "notional": "300000", "currency": "KRW"},  # UTC yday -> KST 05:00 06-17 = today
        {"ts": "2026-06-16T10:00:00+00:00", "decision": "placed", "notional": "999999", "currency": "KRW"},  # KST 19:00 06-16 = yesterday -> skip
        {"ts": "2026-06-17T02:00:00+00:00", "decision": "placed", "notional": "100", "currency": "USD"},
        {"ts": "2026-06-17T03:00:00+00:00", "decision": "previewed", "notional": "50000", "currency": "KRW"},  # not placed -> skip
    ]
    m.restore_spend(events)
    assert m._spent["KRW"] == Decimal("1000000")  # 700,000 + 300,000
    assert m._spent["USD"] == Decimal("100")
```

**Step 1 (c) place 감사** — `test_tools_write.py` 끝에:

```python
def test_place_audit_records_currency_and_notional(app_factory):
    app = app_factory(mode="paper")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="10", price="70000")
    T.place_order(app, confirmation_token=pv["confirmationToken"])
    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    placed = [json.loads(l) for l in lines if json.loads(l)["decision"] == "placed"][0]
    assert placed["currency"] == "KRW"
    assert placed["notional"] == "700000"
```

**Step 1 (d) server 부팅 복원** — `test_server_modes.py` 끝에:

```python
def test_build_server_restores_todays_spend(tmp_path):
    from datetime import datetime, timezone
    from tossinvest_mcp.server import build_app_context
    audit_path = tmp_path / "audit.log"
    ts = datetime.now(timezone.utc).isoformat()
    audit_path.write_text(
        f'{{"ts": "{ts}", "tool": "place_order", "decision": "placed", '
        f'"notional": "700000", "currency": "KRW"}}\n', encoding="utf-8")
    settings = Settings(_env_file=None, mode="paper", audit_log_path=str(audit_path))
    app = build_app_context(settings, client=FakeClient())
    assert app.safety._spent["KRW"] == Decimal("700000")
```

(파일 상단 import 에 `from decimal import Decimal` 가 필요 — `test_server_modes.py` 에 추가.)

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_audit.py tossinvest-mcp/tests/test_safety_tokens.py::test_restore_spend_sums_todays_placed_by_currency "tossinvest-mcp/tests/test_tools_write.py::test_place_audit_records_currency_and_notional" tossinvest-mcp/tests/test_server_modes.py::test_build_server_restores_todays_spend -v`
Expected: FAIL — `read_events`/`restore_spend` 없음, place 감사에 currency/notional 없음, 부팅 복원 미연결.

- [ ] **Step 3: 구현 (a) `audit.py::read_events`** — `record` 메서드 뒤에 추가:

```python
    def read_events(self) -> list[dict]:
        """Parse the JSONL audit file into events (missing file -> [], bad lines skipped)."""
        if not self._path.exists():
            return []
        events: list[dict] = []
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
        return events
```

- [ ] **Step 4: 구현 (b) `safety.py::restore_spend` + KST import** — 파일 상단 import 에 추가 (`from datetime import date` 줄을 교체):

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo
```

그리고 모듈 상수(`HIGH_VALUE_THRESHOLD` 정의 위, import 아래)에:

```python
_KST = ZoneInfo("Asia/Seoul")
```

그리고 `SafetyManager.restore_spend` 를 `record_spend` 뒤에 추가:

```python
    def restore_spend(self, events: list[dict]) -> None:
        """Rebuild today's per-currency spend from prior 'placed' audit events (UTC ts -> KST date)."""
        self._roll_daily()
        today = self._today()
        for ev in events:
            if ev.get("decision") != "placed":
                continue
            notional = ev.get("notional")
            ts = ev.get("ts")
            if notional is None or ts is None:
                continue
            try:
                ev_date = datetime.fromisoformat(ts).astimezone(_KST).date()
            except (ValueError, TypeError):
                continue
            if ev_date != today:
                continue
            currency = ev.get("currency", "KRW")
            self._spent[currency] = self._spent.get(currency, Decimal("0")) + to_decimal(notional)
```

- [ ] **Step 5: 구현 (c) place 감사 레코드** — `tools.py::place_order` 의 성공 감사 `app.audit.record({...})` 를 교체:

```python
    app.audit.record({
        "tool": "place_order", "mode": app.config.mode, "decision": "placed",
        "result": result, "clientOrderId": spec.client_order_id,
        "currency": spec.currency, "notional": spec.notional,
    })
```

- [ ] **Step 6: 구현 (d) server 부팅 와이어링** — `server.py::build_app_context` 에서 `audit = AuditLog(settings.audit_log_path)` 줄 뒤, `return AppContext(...)` 앞에 삽입:

```python
    safety.restore_spend(audit.read_events())  # rebuild today's spend across restarts
```

- [ ] **Step 7: 통과 확인 (관련 4파일)**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_audit.py tossinvest-mcp/tests/test_safety_tokens.py tossinvest-mcp/tests/test_tools_write.py tossinvest-mcp/tests/test_server_modes.py -v`
Expected: 신규 + 기존 PASS (`test_preview_then_place_fills_paper_and_audits` 는 `tool` 필드만 검사 → 무회귀).

- [ ] **Step 8: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/audit.py tossinvest-mcp/src/tossinvest_mcp/safety.py tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/src/tossinvest_mcp/server.py tossinvest-mcp/tests/test_audit.py tossinvest-mcp/tests/test_safety_tokens.py tossinvest-mcp/tests/test_tools_write.py tossinvest-mcp/tests/test_server_modes.py
# 메시지: "feat(safety): restore today's per-currency spend from audit log on boot"
```

---

## Task 9: MCP — 2단계 modify + cancel previous-status 감사 (#1, #10)

`modify_order` 를 `preview_modify`→`modify_order(confirmation_token)` 2단계로(place 와 동형). paper 는 여전히 live 전용(`PaperError`). modify 는 정정 후 notional 에 대해 가드레일 검사하되 일일누적 미가산(M1: `check_daily=False` + `release`). `cancel_order` 는 단일 단계 유지하되 `get_order` 로 이전상태 감사.

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/tools.py` (`modify_order` 교체, `preview_modify` 신규, `cancel_order` 교체)
- Modify: `tossinvest-mcp/src/tossinvest_mcp/server.py` (`_register_writes`: `preview_modify` 등록 + `modify_order` 시그니처 교체)
- Modify: `tossinvest-mcp/tests/conftest.py` (`FakeClient.get_order` 현실화)
- Test: `tossinvest-mcp/tests/test_tools_write.py` (재작성 1 + 신규), `tossinvest-mcp/tests/test_server_modes.py` (WRITE_TOOLS)

**Interfaces:**
- Consumes: Task 4 `build_spec(..., modify_order_id=...)`/`spec.modify_order_id`, Task 5 `check_guardrails(..., check_daily=False)`, Task 7 `release(token)`, `app.client.get_order/modify_order`, `_market_gate`, `app.safety.consume/issue_token`.
- Produces:
  - `preview_modify(app, order_id, *, order_type, price=None, quantity=None, confirm_high_value_order=False) -> dict` — paper `PaperError`; live: `get_order` 머지 → `build_spec(modify_order_id=order_id)` → `check_guardrails(..., check_daily=False)` + 장운영 게이트 → `issue_token` → `"modify_previewed"` 감사(previousStatus 포함) → `{confirmationToken, orderId, symbol, side, orderType, estimatedNotional, expiresInSec, mode}`.
  - `modify_order(app, *, confirmation_token) -> dict` — `consume` → `check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=False)` → `client.modify_order(spec.modify_order_id, ...)` → 성공 시 `release(token)` + `"modified"` 감사 / 실패 시 토큰 유지 + `"error"` 감사.
  - `cancel_order(app, order_id) -> dict` — paper `PaperError`; live: `get_order`(previousStatus) → `client.cancel_order` → `"canceled"` 감사(previousStatus 포함).
  - server: `preview_modify` 툴 등록, `modify_order` 툴은 `(confirmation_token)` 시그니처.

- [ ] **Step 1: conftest `FakeClient.get_order` 현실화** — `tossinvest-mcp/tests/conftest.py` 의 `get_order` 교체:

```python
    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return {"orderId": order_id, "symbol": "005930", "side": "BUY",
                "orderType": "LIMIT", "quantity": "10", "price": "70000", "status": "PENDING"}
```

(기존 `status: "PENDING"` 유지 → cancel #10·기존 paper get_order 테스트 무회귀. MCP/SDK 어느 테스트도 real get_order 의 정확한 shape 를 단언하지 않음.)

- [ ] **Step 2: 실패 테스트 작성/재작성** — `test_tools_write.py`:

(a) 기존 `test_modify_and_cancel_are_live_only` 를 **교체**:

```python
def test_modify_and_cancel_are_live_only(app_factory):
    app = app_factory(mode="paper")
    with pytest.raises(PaperError):
        T.preview_modify(app, "paper-1", order_type="LIMIT", price="71000")
    with pytest.raises(PaperError):
        T.cancel_order(app, "paper-1")
```

(b) 파일 끝에 신규:

```python
def test_preview_modify_live_issues_token_with_merged_notional(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")  # qty 10 (orig) * 71000
    assert pv["confirmationToken"]
    assert pv["orderId"] == "real-1"
    assert pv["estimatedNotional"] == "710000"


def test_preview_then_modify_calls_client_and_releases_token(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    out = T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert out["orderId"] == "real-2"
    call = [c for c in fake_client.calls if c[0] == "modify_order"][-1]
    assert call[2]["price"] == "71000"
    with pytest.raises(GuardrailError):  # token released -> second modify fails
        T.modify_order(app, confirmation_token=pv["confirmationToken"])


def test_modify_does_not_touch_daily_bucket(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False)
    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    T.modify_order(app, confirmation_token=pv["confirmationToken"])
    assert app.safety._spent["KRW"] == Decimal("0")  # M1: modify never accumulates


def test_preview_modify_enforces_per_order_cap(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False,
                      max_order_amount="100000")
    with pytest.raises(GuardrailError) as e:  # 10 * 71000 = 710,000 > 100,000
        T.preview_modify(app, "real-1", order_type="LIMIT", price="71000")
    assert e.value.code == "order-amount-cap"


def test_cancel_records_previous_status(app_factory, fake_client):
    app = app_factory(mode="live", allow_live=True)
    T.cancel_order(app, "real-1")
    lines = open(app.config.audit_log_path, encoding="utf-8").read().strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["decision"] == "canceled"
    assert entry["previousStatus"] == "PENDING"
```

(`test_tools_write.py` 상단에 `from decimal import Decimal` 추가 필요.)

(c) `test_server_modes.py::WRITE_TOOLS` 에 `preview_modify` 추가:

```python
WRITE_TOOLS = {"get_order_readiness", "preview_order", "place_order",
               "preview_modify", "modify_order", "cancel_order"}
```

- [ ] **Step 3: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_write.py tossinvest-mcp/tests/test_server_modes.py -v`
Expected: FAIL — `T.preview_modify` 없음, `modify_order(confirmation_token=...)` 시그니처 불일치, WRITE_TOOLS 에 preview_modify 미등록.

- [ ] **Step 4: 구현 (a) `tools.py::preview_modify` 신규** — `tools.py` 의 `def modify_order(...)` **앞**에 추가:

```python
def preview_modify(app: AppContext, order_id: str, *, order_type: str,
                   price: "str | None" = None, quantity: "str | None" = None,
                   confirm_high_value_order: bool = False) -> dict:
    if app.use_paper:
        from .paper import PaperError
        raise PaperError("paper mode fills orders immediately; modify is live-only")
    original = app.client.get_order(order_id)
    symbol = original.get("symbol")
    side = original.get("side")
    merged_price = price if price is not None else original.get("price")
    merged_qty = quantity if quantity is not None else original.get("quantity")
    spec = app.safety.build_spec(
        symbol=symbol, side=side, order_type=order_type,
        quantity=merged_qty, price=merged_price,
        confirm_high_value_order=confirm_high_value_order, modify_order_id=order_id,
    )
    is_open, enforce = _market_gate(app, symbol)
    app.safety.check_guardrails(spec, is_market_open=is_open, enforce_hours=enforce,
                                check_daily=False)  # M1: per-order gated, no daily bucket
    token = app.safety.issue_token(spec)
    app.audit.record({
        "tool": "preview_modify", "mode": app.config.mode, "decision": "modify_previewed",
        "orderId": order_id, "previousStatus": original.get("status"),
        "symbol": symbol, "side": side, "notional": spec.notional,
        "clientOrderId": spec.client_order_id, "token": token,
    })
    return {
        "confirmationToken": token,
        "orderId": order_id,
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "estimatedNotional": str(spec.notional),
        "expiresInSec": app.config.confirmation_ttl_sec,
        "mode": app.config.mode,
    }
```

- [ ] **Step 5: 구현 (b) `tools.py::modify_order` 2단계로 교체** — 기존 `modify_order` 전체 교체:

```python
def modify_order(app: AppContext, *, confirmation_token: str) -> dict:
    spec = app.safety.consume(confirmation_token)  # validates exists + not expired
    # re-check amount guardrails on the amended order (M1: no daily bucket add/check)
    app.safety.check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=False)
    try:
        result = app.client.modify_order(
            spec.modify_order_id, order_type=spec.order_type,
            price=spec.price, quantity=spec.quantity,
            confirm_high_value_order=spec.confirm_high_value_order,
        )
    except Exception as e:
        app.audit.record({
            "tool": "modify_order", "mode": app.config.mode, "decision": "error",
            "error": str(e), "orderId": spec.modify_order_id,
            "clientOrderId": spec.client_order_id,
        })
        raise  # token NOT released -> idempotent retry reuses same clientOrderId

    app.safety.release(confirmation_token)  # pop only, no daily accrual (M1)
    app.audit.record({
        "tool": "modify_order", "mode": app.config.mode, "decision": "modified",
        "orderId": spec.modify_order_id, "result": result,
        "clientOrderId": spec.client_order_id,
    })
    return result
```

- [ ] **Step 6: 구현 (c) `tools.py::cancel_order` 이전상태 감사** — 기존 `cancel_order` 전체 교체:

```python
def cancel_order(app: AppContext, order_id: str) -> dict:
    if app.use_paper:
        from .paper import PaperError
        raise PaperError("paper mode fills orders immediately; cancel is live-only")
    previous = app.client.get_order(order_id)
    result = app.client.cancel_order(order_id)
    app.audit.record({"tool": "cancel_order", "mode": app.config.mode,
                      "decision": "canceled", "orderId": order_id,
                      "previousStatus": previous.get("status"), "result": result})
    return result
```

- [ ] **Step 7: 구현 (d) server 등록** — `server.py::_register_writes` 의 기존 `modify_order` `@mcp.tool` 블록을 아래 **두 툴**로 교체:

```python
    @mcp.tool(name="preview_modify",
              description="STEP 1 of 2 to modify a LIVE open order. Merges the amendment with the "
                          "original order, validates it against guardrails, and returns a "
                          "confirmation_token. live only. Money/quantity are strings.")
    def preview_modify(order_id: str, order_type: str, price: "str | None" = None,
                       quantity: "str | None" = None, confirm_high_value_order: bool = False) -> dict:
        return T.preview_modify(app, order_id, order_type=order_type, price=price,
                                quantity=quantity, confirm_high_value_order=confirm_high_value_order)

    @mcp.tool(name="modify_order",
              description="STEP 2 of 2. Apply the modification validated by preview_modify, using its "
                          "confirmation_token (returns a NEW orderId). live only; idempotent.")
    def modify_order(confirmation_token: str) -> dict:
        return T.modify_order(app, confirmation_token=confirmation_token)
```

- [ ] **Step 8: 통과 확인 (write + server_modes + 전체 MCP)**

Run:
```bash
uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_tools_write.py tossinvest-mcp/tests/test_server_modes.py -v
uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q
```
Expected: 신규/재작성 PASS, 전체 MCP 그린(이제 14 툴).

- [ ] **Step 9: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/src/tossinvest_mcp/tools.py tossinvest-mcp/src/tossinvest_mcp/server.py tossinvest-mcp/tests/conftest.py tossinvest-mcp/tests/test_tools_write.py tossinvest-mcp/tests/test_server_modes.py
# 메시지: "feat(tools): two-step preview_modify->modify_order gate + cancel previous-status audit"
```

---

## Task 10: 문서 — README live human-in-the-loop 경고 + config 행 (#4 docs)

`tossinvest-mcp/README.md` 에 ⚠️ "live + 자동승인 클라이언트 금지" 경고와 신규 env(USD 상한·`LIVE_CONFIRM_MIN_DELAY_SEC`) 추가. preview/place·modify 2단계 반영.

**Files:**
- Modify: `tossinvest-mcp/README.md`

**Interfaces:** (문서 — 코드 인터페이스 없음)

- [ ] **Step 1: 안전모델 절에 live 경고 추가** — `### 2. 2단계 주문 — human-in-the-loop` 블록 끝(다음 `### 3` 앞)에 추가:

```markdown
> ⚠️ **live 모드 = 사람 승인이 마지막 방어선.** `live` 에서는 MCP 클라이언트의 **툴 승인 UX(사람이 각 호출 승인)** 가 꼭 켜져 있어야 합니다. **자동승인(auto-approve)/무인 에이전트에 live 키를 절대 붙이지 마세요.** 서버단 가드레일은 1차 방어일 뿐, 거액·오발주의 최종 차단은 사람입니다. 무인 운용이 불가피하면 `LIVE_CONFIRM_MIN_DELAY_SEC`(예: `5`)로 preview→place 사이 최소 지연을 강제해 즉발 체결을 막으세요.
```

- [ ] **Step 2: 쓰기 툴 표에 `preview_modify` 추가 + `modify_order` 행 갱신** — `### 쓰기` 표의 `modify_order` 행을 아래로 교체:

```markdown
| `preview_modify` | `(order_id, order_type, price=None, quantity=None, confirm_high_value_order=False)` | **STEP 1/2(정정).** 원주문과 머지해 가드레일 검사 → `confirmationToken`. **live 전용** |
| `modify_order` | `(confirmation_token)` | **STEP 2/2(정정).** 토큰으로 정정(새 orderId). **live 전용**, 멱등 |
```

그리고 "## 13개 툴" 제목과 배지를 14 로:

```markdown
## 14개 툴
```

(배지 줄 `![tests](https://img.shields.io/badge/tests-64%20passing-2ea44f)` 의 숫자는 Task 11 의 최종 카운트로 함께 갱신.)

- [ ] **Step 3: config 표에 신규 env 3행 추가** — 설정 표의 `DAILY_ORDER_LIMIT` 행 뒤에:

```markdown
| `MAX_ORDER_AMOUNT_USD` | `1000` | 주문당 상한 (USD 표기 종목 = 알파벳 심볼) |
| `DAILY_ORDER_LIMIT_USD` | `5000` | 일일 누적 상한 (USD) |
```

그리고 `CONFIRMATION_TTL_SEC` 행 뒤에:

```markdown
| `LIVE_CONFIRM_MIN_DELAY_SEC` | `0` | **live 전용** preview→place 최소 지연(초). `0`=off, 무인 운용 시 `5` 권장 |
```

- [ ] **Step 4: 통화별 가드레일 설명 보강** — `### 3. 가드레일` 의 한 줄을 교체:

```markdown
주문당/일일 누적 금액 상한 · 종목 allow/deny · **고액 명시적 확인 필수** · **하드실링 즉시 거부** · 장운영시간(live 전용). 임계는 **주문통화별**(KRW: 1억 확인/30억 거부, USD: $10만 확인/$300만 거부 — 알파벳 심볼=USD, 숫자 심볼=KRW, FX 환산 없음).
```

- [ ] **Step 5: 검토 (렌더 확인 — 테스트 없음)**

Run: `git diff --stat tossinvest-mcp/README.md`
Expected: README.md 변경만. (표/경고 마크다운 깨짐 없는지 육안 확인.)

- [ ] **Step 6: 스테이징 + 메시지 준비**

```bash
git add tossinvest-mcp/README.md
# 메시지: "docs(mcp): live human-in-the-loop warning, USD caps & min-delay env, two-step modify"
```

---

## Task 11: 문서 자가갱신 — `CLAUDE.md` 안전 불변식 + `docs/claude/tossinvest-mcp.md`

코드가 진실. 안전 불변식 변화(2단계 modify·통화별 한도·부팅 복원·양수검증·live 지연)와 14 툴·신규 env·새 함정을 living 문서에 반영. (자가갱신 규칙 — 같은 세션, 커밋은 수동.)

**Files:**
- Modify: `CLAUDE.md` (CRITICAL RULES 의 `place_order` 불변식 항목 — modify 게이트화 한 줄, Conventions 의 MCP 안전모델 — 통화별 한도/2단계 modify/부팅복원, env 목록에 USD·min-delay, 함정에 새 항목)
- Modify: `docs/claude/tossinvest-mcp.md` (가드레일 통화별, preview→place/modify 토큰 생애, 14 툴, config, 함정, 새 툴 추가 절차 영향)

**Interfaces:** (문서)

- [ ] **Step 1: `CLAUDE.md` — `place_order` 안전 불변식 항목에 modify 추가**. CRITICAL RULES 의 `**place_order 안전 불변식 ...` 항목 끝에 문장 추가:

```markdown
 **modify 도 동형 2단계**(`preview_modify`→`modify_order(confirmation_token)`): consume → 가드레일 재검사(`check_daily=False`, M1 — 일일누적 미가산) → 실행 → 성공 시 `release`(pop only) / 실패 시 토큰 유지. 우회 금지.
```

- [ ] **Step 2: `CLAUDE.md` — Conventions 의 MCP 안전모델 줄 갱신**. "가드레일(주문당·일일 상한·allow/deny·1억↑ confirm 필수·30억↑ 거부·장시간 게이트는 live 전용)." 를 아래로 교체:

```markdown
가드레일(**주문통화별** 주문당·일일 상한·allow/deny·고액 confirm 필수·하드실링 거부 — KRW 1억/30억, USD $10만/$300만, 알파벳=USD·숫자=KRW·FX 환산 X; 장시간 게이트는 live 전용). preview→place / preview_modify→modify 2단계 + consume-on-success 멱등성(modify 는 `release`) + place 시 일일한도 재검사 + 부팅 시 감사로그로 당일 누적 복원 + 감사로그(JSONL).
```

- [ ] **Step 3: `CLAUDE.md` — 설정 항목에 신규 env**. Conventions 의 `설정` 줄 env 목록 `.../ENFORCE_MARKET_HOURS)` 를 `.../ENFORCE_MARKET_HOURS/MAX_ORDER_AMOUNT_USD/DAILY_ORDER_LIMIT_USD/LIVE_CONFIRM_MIN_DELAY_SEC)` 로 확장.

- [ ] **Step 4: `CLAUDE.md` — 함정 절에 새 항목 추가**. "## 주의할 함정" 목록 끝에:

```markdown
- **통화 판정은 심볼 모양** — 알파벳 심볼=USD, 숫자=KRW(FX 환산 없음). KRW/USD 일일누적 버킷이 분리돼 한 통화 한도가 다른 통화를 막지 않는다. notional 단위는 주문통화.
- **modify 일일누적 미가산(M1)** — modify 는 정정 후 금액에 주문당/고액/하드실링/allow-deny 만 검사(`check_daily=False`), 일일 버킷엔 가산·검사 안 함. 델타 회계 없음.
- **_spent 부팅 복원** — `place` 감사에 `currency`+`notional` 기록, 서버 시작 시 `audit.read_events()`→`safety.restore_spend` 가 당일(UTC ts→KST 날짜) `placed` 합산. 감사 파일 지우면 당일 누적도 리셋됨(주의).
```

- [ ] **Step 5: `docs/claude/tossinvest-mcp.md` 갱신** — 아래를 반영(코드와 일치하게):
  - "## 가드레일" 절: 통화별 임계 세트(KRW/USD 상수·config), `check_daily` 플래그, 순서 불변 재확인.
  - "## preview → place 토큰 생애" 절: `consume` 의 live 최소지연 게이트, `finalize`(place, 통화 유도) vs `release`(modify, 미가산), place 시 재검사 추가.
  - "## 13 툴" → "## 14 툴": `preview_modify` 추가, `modify_order` 시그니처 `(confirmation_token)` 로, cancel previousStatus 감사 명시.
  - "## 모듈별 함정": 통화 판정/ M1 modify / 부팅 복원(ts UTC→KST) / 양수검증(`invalid-order-value`) 추가.
  - "## config" 절: `max_order_amount_usd`(1,000,000? → 1000)·`daily_order_limit_usd`(5000)·`live_confirm_min_delay_sec`(0) 추가.
  - 헤더의 "64개" 등 테스트 카운트 언급은 최종 그린 수로 갱신.

- [ ] **Step 6: 전체 테스트 최종 그린 + 카운트 확인** (문서의 숫자 근거)

Run:
```bash
uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q
uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q
```
Expected: 둘 다 PASS. 출력의 `N passed` 를 README 배지/문서 카운트에 반영(SDK 44, MCP 신규 합계).

- [ ] **Step 7: 스테이징 + 메시지 준비**

```bash
git add CLAUDE.md docs/claude/tossinvest-mcp.md tossinvest-mcp/README.md
# 메시지: "docs: self-update safety invariants (two-step modify, per-currency caps, boot restore, 14 tools)"
```

---

## 최종 검증 (적대적 리뷰 3종 — 실행 후)

전 태스크 그린 후, **적대적 리뷰어 3명을 병렬**로 돌려 구멍이 닫혔는지 확인(가드레일 / 프롬프트인젝션 / SDK). 각 리뷰어는 아래를 적대적으로 시도:
- **가드레일**: 통화 혼동으로 USD 한도 우회, modify 로 일일한도 우회(M1 의도대로 닫혔나), place 재검사 우회, 음수/0 주문, 부팅 복원 누락/중복.
- **프롬프트인젝션**: LLM 이 토큰 위조/재사용, preview 없이 place/modify, confirm_high_value 강제 세팅, deny 우회.
- **SDK**: 200 비정상응답·토큰 만료 경계·result 부재가 조용히 통과하는지.

발견 시 해당 태스크 패턴으로 추가 테스트→수정→그린.

## Self-Review (이 플랜 작성자 체크)

- **Spec coverage**: #1 modify게이트=Task9, #2 place재검사=Task6, #3 통화별=Task4·5, #4 live지연=Task7·10, #5 부팅복원=Task8, #6 양수검증=Task3, #7·#8 SDK응답=Task2, #9 토큰만료=Task1, #10 cancel/preview 이전상태감사=Task9. **10/10 매핑됨.**
- **Placeholder scan**: 모든 코드 스텝에 실제 코드 포함, "TODO/적절히 처리" 없음.
- **Type consistency**: `check_guardrails(..., check_daily=True)` (Task5) 를 Task6(place 재검사 default True)·Task9(modify/preview `check_daily=False`)에서 동일 시그니처로 사용. `build_spec(..., modify_order_id=None)`·`OrderSpec.currency/modify_order_id`(Task4) 를 Task9 에서 사용. `release`(Task7) 를 Task9 에서 사용. `restore_spend`/`read_events`(Task8) 와이어링 일관.

---

# Round 2 — 적대적 리뷰 후속 수정 (사용자 승인 범위)

전 11 태스크 그린 후 적대적 리뷰 3종이 추가 발견. 사용자 결정: 아래 4건 이번 브랜치에서 수정, C1(통화=심볼모양 한계)은 알려진 한계로 문서화+후속. 같은 TDD 흐름.

## Task 12: 가드레일 입력 견고화 — order_amount 조합 거부 (C2) + 심볼매칭 정규화

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`build_spec` 조합 거부; `check_guardrails` deny/allow 비교 정규화)
- Test: `tossinvest-mcp/tests/test_safety_guardrails.py`

**Interfaces:**
- Produces: `build_spec` rejects `order_amount` together with `price` or `quantity` → `GuardrailError("invalid-order-params")`. deny/allow 비교는 `symbol.strip().upper()` 정규화(양쪽). **spec.symbol 은 변형하지 않음**(broker 전송값 불변 — C1 별개).

- [ ] **Step 1: 실패 테스트** — `test_safety_guardrails.py` 끝에:

```python
def test_build_spec_rejects_order_amount_with_price():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="AAPL", side="BUY", order_type="LIMIT",
                     order_amount="100", price="1000000", quantity="1000")
    assert e.value.code == "invalid-order-params"


def test_build_spec_rejects_order_amount_with_quantity():
    m = _mgr()
    with pytest.raises(GuardrailError) as e:
        m.build_spec(symbol="AAPL", side="BUY", order_type="MARKET",
                     order_amount="100", quantity="1000")
    assert e.value.code == "invalid-order-params"


def test_deny_list_matches_whitespace_and_case_insensitive():
    m = _mgr(deny_symbols=["AAPL"])
    with pytest.raises(GuardrailError) as e:
        _ok(m, _spec(m, symbol=" aapl "))  # evasion attempt
    assert e.value.code == "symbol-denied"


def test_allow_list_normalizes_symbol():
    m = _mgr(allow_symbols=["aapl"])      # config lowercase
    _ok(m, _spec(m, symbol="AAPL"))       # must pass (normalized match)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests/test_safety_guardrails.py -k "order_amount_with or normaliz or whitespace_and_case" -v`
Expected: FAIL — combo not rejected; deny/allow exact-match misses normalized forms.

- [ ] **Step 3: 구현 (a) C2 조합 거부** — `safety.py::build_spec`, 비양수 검증 루프(`for label, val ...`) 뒤·`if order_amount is not None:` notional 블록 앞에 삽입:

```python
        if order_amount is not None and (price is not None or quantity is not None):
            raise GuardrailError(
                "invalid-order-params",
                "order_amount cannot be combined with price or quantity",
            )
```

- [ ] **Step 4: 구현 (b) deny/allow 정규화** — `check_guardrails` 의 deny/allow 두 줄을 교체:

```python
        sym = spec.symbol.strip().upper()
        if cfg.deny_symbols and sym in {s.strip().upper() for s in cfg.deny_symbols}:
            raise GuardrailError("symbol-denied", f"{spec.symbol} is in the deny list")
        if cfg.allow_symbols and sym not in {s.strip().upper() for s in cfg.allow_symbols}:
            raise GuardrailError("symbol-not-allowed", f"{spec.symbol} is not in the allow list")
```

- [ ] **Step 5: 통과 확인** — `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q` (기존 deny/allow 정확매칭 테스트는 정규화로 무회귀).
- [ ] **Step 6: Commit** — `git add` safety.py + test_safety_guardrails.py; msg: `feat(safety): reject order_amount+price/quantity combo and normalize symbol matching`

## Task 13: restore_spend 악성/손상 감사로그 견고화 (부팅 DoS)

**Files:**
- Modify: `tossinvest-mcp/src/tossinvest_mcp/safety.py` (`from decimal import Decimal` → `Decimal, InvalidOperation`; `restore_spend` per-event 가드)
- Test: `tossinvest-mcp/tests/test_safety_tokens.py`

**Interfaces:**
- Produces: `restore_spend` skips non-dict events and events whose `notional` fails `to_decimal` (`InvalidOperation`/`TypeError`) without crashing; valid events still summed.

- [ ] **Step 1: 실패 테스트** — `test_safety_tokens.py` 끝에:

```python
def test_restore_spend_skips_malformed_events_without_crashing():
    s = Settings(_env_file=None)
    m = SafetyManager(s, now=lambda: 1000.0, today=lambda: date(2026, 6, 17))
    events = [
        {"ts": "2026-06-17T01:00:00+00:00", "decision": "placed", "notional": "700000", "currency": "KRW"},
        {"ts": "2026-06-17T02:00:00+00:00", "decision": "placed", "notional": "abc", "currency": "KRW"},  # bad value
        [1, 2, 3],  # non-dict line
        {"ts": "2026-06-17T03:00:00+00:00", "decision": "placed", "notional": "300000", "currency": "KRW"},
    ]
    m.restore_spend(events)  # must not raise
    assert m._spent["KRW"] == Decimal("1000000")  # 700,000 + 300,000; bad ones skipped
```

- [ ] **Step 2: 실패 확인** — `uv run --package tossinvest-mcp pytest "tossinvest-mcp/tests/test_safety_tokens.py::test_restore_spend_skips_malformed_events_without_crashing" -v` → FAIL (`InvalidOperation`/`AttributeError`).
- [ ] **Step 3: 구현 (a) import** — `safety.py` 상단 `from decimal import Decimal` → `from decimal import Decimal, InvalidOperation`.
- [ ] **Step 4: 구현 (b) restore_spend 가드** — 메서드의 for 루프를 교체:

```python
        for ev in events:
            if not isinstance(ev, dict) or ev.get("decision") != "placed":
                continue
            notional = ev.get("notional")
            ts = ev.get("ts")
            if notional is None or ts is None:
                continue
            try:
                ev_date = datetime.fromisoformat(ts).astimezone(_KST).date()
                amount = to_decimal(notional)
            except (ValueError, TypeError, InvalidOperation):
                continue
            if ev_date != today:
                continue
            currency = ev.get("currency", "KRW")
            self._spent[currency] = self._spent.get(currency, Decimal("0")) + amount
```

- [ ] **Step 5: 통과 확인** — `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q` (기존 restore 테스트 무회귀).
- [ ] **Step 6: Commit** — `git add` safety.py + test_safety_tokens.py; msg: `fix(safety): make boot spend-restore robust to malformed audit events`

## Task 14: SDK — 200 경로 RecursionError 가드 (깊은 중첩 JSON)

**Files:**
- Modify: `pytossinvest/src/pytossinvest/client.py` (200 분기의 `except ValueError` → `except (ValueError, RecursionError)`)
- Test: `pytossinvest/tests/test_client_core.py`

**Interfaces:**
- Produces: deeply-nested 200 JSON body raising `RecursionError` during parse → `TossInvestError("invalid-response")` (server no longer crashes).

- [ ] **Step 1: 실패 테스트** — `test_client_core.py` 끝에:

```python
@respx.mock
def test_200_deeply_nested_json_raises_invalid_response():
    from pytossinvest.errors import TossInvestError
    _token_route()
    deep = b"[" * 5000 + b"]" * 5000  # exceeds recursion limit during json parse
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(200, content=deep))
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert exc.value.code == "invalid-response"
```

- [ ] **Step 2: 실패 확인** — `uv run --package pytossinvest --extra dev pytest "pytossinvest/tests/test_client_core.py::test_200_deeply_nested_json_raises_invalid_response" -v` → FAIL (uncaught `RecursionError`).
- [ ] **Step 3: 구현** — `client.py` 200 분기의 `except ValueError:` 줄을 `except (ValueError, RecursionError):` 로 교체(invalid-response 발생부).
- [ ] **Step 4: 통과 확인** — `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q` (전체 SDK 무회귀).
- [ ] **Step 5: Commit** — `git add` client.py + test_client_core.py; msg: `fix(client): catch RecursionError from deeply-nested 200 JSON as invalid-response`

## Task 15: 문서 — C1 알려진 한계 + Round 2 반영

**Files:**
- Modify: `CLAUDE.md`, `docs/claude/tossinvest-mcp.md`

- [ ] **Step 1:** `CLAUDE.md` 함정 절의 통화 판정 항목에 **C1 알려진 한계** 한 줄 추가: 점/접미사 포함 US 티커(`BRK.B` 등)·공백 변형은 `isalpha()` 로 KRW 판정되어 KRW 임계가 적용됨(회귀 아님 — 이전엔 전부 KRW). 정확한 통화는 권위 데이터(`get_stocks`/`get_prices`의 currency) 기반 후속 PR 과제. 외부의존 0 결정 유지.
- [ ] **Step 2:** `CLAUDE.md` + `docs/claude/tossinvest-mcp.md` 에 Round 2 사실 반영: 새 에러코드 `invalid-order-params`(order_amount+price/quantity 동시 거부), deny/allow 매칭은 대소문자·공백 무시(정규화, spec.symbol 자체는 불변), restore_spend 손상 이벤트 skip(부팅 견고), SDK 200 경로가 `RecursionError`도 invalid-response 로 처리.
- [ ] **Step 3:** 최종 카운트 재확인 후 모든 문서/README 의 테스트 수 갱신(SDK·MCP 최종 그린 수).
- [ ] **Step 4: Commit** — `git add` CLAUDE.md docs/claude/tossinvest-mcp.md (+ 카운트 바뀐 README); msg: `docs: document currency-by-symbol limitation and round-2 hardening`
