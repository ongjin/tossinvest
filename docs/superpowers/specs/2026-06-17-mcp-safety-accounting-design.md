# MCP 안전 회계 설계 — C1 권위 통화 + M1 modify 델타 회계

**날짜:** 2026-06-17
**대상 패키지:** `tossinvest-mcp` (`safety.py`, `tools.py`)
**상태:** 설계 확정 (구현 플랜 대기)
**선행:** safety-hardening 브랜치(Round 1 + Round 2) 머지 완료. 본 스펙은 거기서 "알려진 한계"로 남긴 **C1**(통화=심볼모양)·**M1**(modify 일일누적 미가산) 두 건의 후속.

---

## 1. 배경 / 문제

safety-hardening 종료 시점에 두 개의 알려진 한계를 의도적으로 후속으로 남겼다.

- **C1 — 통화 판정이 심볼 모양에만 의존.** `safety.order_currency(symbol)` 은 `symbol.isalpha()` 한 줄로 USD/KRW 를 가른다. `BRK.B` 처럼 점·접미사가 붙은 US 티커, 공백 변형은 `isalpha()` 가 `False` → KRW 로 오분류 → KRW 임계(1억/30억)·KRW 일일버킷이 USD 종목에 적용된다. 안전 상한이 환율·네트워크에 의존하지 않게 하려고 "외부 의존 0" 을 의도적으로 택했던 결과이자 그 대가.
- **M1 — modify 가 일일 누적을 우회.** modify 경로는 `check_daily=False` 로 일일 버킷을 검사·가산하지 않고 성공 시 `release`(pop only) 한다. 작은 주문을 낸 뒤 modify 로 금액을 키우면 일일 한도를 우회할 수 있는 구조적 갭. 델타 회계가 없다.

두 건은 통화 축에서 얽힌다 — M1 의 델타를 **어느 통화 버킷**에 넣을지가 C1 의 통화 판정에 의존한다. 따라서 한 스펙으로 묶어 함께 구현한다.

## 2. 목표 / 비목표

**목표**
- preview 시점에 권위 통화(API `currency`)로 임계·버킷을 선택하되, 조회 실패 시 현행 심볼모양으로 안전하게 강등.
- modify 가 일일 버킷에 **완전 델타 회계**(증가는 검사+가산, 감소는 credit, 0 하한)를 적용하고 재부팅 너머 복원.
- SDK 공개 API 불변. 기존 테스트 무회귀.

**비목표**
- FX 환산 도입(여전히 안 함 — 통화별 비교 유지).
- `_market_gate` 의 국가(US/KR) 판정 개선(장시간 게이트, live 전용, 별개 관심사 — §6 에 관련 항목으로 문서화만).
- 심볼당 통화 캐싱(YAGNI — preview 는 대화형 저빈도).

## 3. C1 — preview 권위 통화 조회 + 심볼모양 폴백

### 3.1 통화 결정 흐름
- `preview_order` / `preview_modify` 에서 `get_prices([symbol])` 를 **한 번** 호출해 `Price.currency` 로 통화를 결정한다.
- `preview_order` 의 MARKET+무금액 경로는 이미 `_ref_price` 로 시세를 받는다 → **같은 한 번의 호출에서 ref price 와 currency 를 함께** 추출해 중복 호출을 만들지 않는다. (LIMIT·금액주문 경로는 통화 결정을 위해 1 GET 이 새로 추가됨 — preview 는 대화형이라 허용)
- **폴백:** 조회 실패(예외) / 빈 결과 / `currency` 누락·공백 → 기존 `order_currency(symbol)`(심볼모양)으로 강등. 즉 현재 동작이 안전한 하한선이며 부팅·네트워크 장애가 preview 를 깨지 않는다.

### 3.2 주입 지점
- `SafetyManager.build_spec(..., currency: str | None = None)` 인자 추가.
- 본문: `currency = currency if currency is not None else order_currency(symbol)` → `OrderSpec.currency` 에 저장.
- 하류(`check_guardrails`, `finalize`, 감사 기록)는 이미 `spec.currency` 를 사용하므로 무변경.
- `order_currency` 는 삭제하지 않고 **폴백 휴리스틱**으로 잔존.

### 3.3 spec.symbol 불변
브로커로 전송하는 `spec.symbol` 은 변형하지 않는다(C2/유니코드 정규화 결정과 동일). 통화만 권위 데이터로 교체.

## 4. M1 — 완전 델타 회계

### 4.1 자료구조
- `OrderSpec` 에 `prev_notional: Decimal | None = None` 필드 추가(modify 시 원본 주문의 명목금액).

### 4.2 preview_modify
- 원본 주문에서 N_old 계산: `to_decimal(original["price"]) * to_decimal(original["quantity"])` (둘 다 있을 때). 누락 시 `None` → 델타 계산에서 전액으로 취급(보수적).
- `build_spec(..., currency=<C1 조회>, modify_order_id=order_id)` 로 amended spec 생성 후 `spec.prev_notional = N_old`.
- 가드레일 호출을 `check_daily=True, prev_notional=spec.prev_notional` 로 전환(현행 `check_daily=False` 에서 변경).

### 4.3 check_guardrails 시그니처
```
check_guardrails(self, spec, *, is_market_open, enforce_hours,
                 check_daily=True, prev_notional: Decimal | None = None) -> None
```
- **일일 블록만** 증분을 델타로 계산:
  - `increment = spec.notional if prev_notional is None else spec.notional - prev_notional`
  - `if self._spent[spec.currency] + increment > daily_cap: raise GuardrailError("daily-limit", ...)`
- **per-order 캡 · 고액확인 · 하드실링** 검사는 여전히 **전액 `spec.notional`** 기준(amended 주문의 전체 크기가 절대 임계를 지켜야 함) — 무변경.

### 4.4 modify_order 실행
- `consume(token)` → spec
- `check_guardrails(spec, is_market_open=True, enforce_hours=False, check_daily=True, prev_notional=spec.prev_notional)`
- 성공 시: `delta = spec.notional - (spec.prev_notional or Decimal("0"))` → `finalize(token, delta)` (pop + 델타 가산).
- 실패 시: 토큰 유지(현행 멱등 재시도 동일).
- `release`(pop-only)는 이 변경으로 **고아 → 제거**(modify 가 유일 사용처였음).

### 4.5 record_spend 0 하한
```
self._spent[currency] = max(Decimal("0"), self._spent.get(currency, Decimal("0")) + notional)
```
- place 는 항상 양수 → 무영향. modify 음수 델타가 버킷을 음수로 만들지 못하게.

### 4.6 수용한 한계
일일 버킷은 단순 합계라 "이 주문이 우리 MCP 로 place 됐는지"를 모른다. 앱에서 직접 낸 주문을 modify·다운사이즈하면 N_old 가 버킷에 없는데 credit 되어 한도가 잘못 느슨해질 수 있다. 0 하한으로 음수는 막되, 정확성보다 단순성을 택해 이 경우를 허용한다(사용자 승인).

## 5. 감사 + 부팅 복원

- `preview_modify` 감사 이벤트에 `currency` 추가.
- `modify_order` 성공 감사 이벤트에 `"notional": delta`(부호 포함) + `"currency"` 추가. `AuditLog.record` 가 `json.dumps(..., default=str)` 라 `Decimal` 은 `"-100"` 같은 문자열로 직렬화된다.
- `restore_spend`: `decision == "placed"` 에 더해 **`decision == "modified"` 이벤트의 `notional`(부호 있는 델타)도 합산**. `to_decimal` 은 음수 문자열을 파싱하므로 그대로 동작. 루프 종료 후 통화별 **0 하한**을 적용해 음수 중간합을 방어한다.
- 손상/비dict/`notional` 파싱불가 이벤트는 기존과 동일하게 조용히 skip(부팅 견고성 유지).

## 6. 불변식 / 문서 변경

- **CLAUDE.md CRITICAL RULES** 의 modify 항목: "성공 시 `release`(pop only) … 일일누적 미가산(M1)" → **"성공 시 델타 가산(`finalize(delta)`), 일일 델타 검사+가산"** 으로 수정.
- **CLAUDE.md 함정 절**: C1 항목(이제 권위 통화 + 폴백)·M1 항목(델타 회계) 갱신. `order_amount` 함정 등 나머지는 유지.
- **docs/claude/tossinvest-mcp.md**: 가드레일 통화 판정·modify 토큰 생애·부팅 복원 절 동기화.
- **관련 알려진 항목(범위 밖)**: `_market_gate` 의 `symbol.isalpha()` 국가판정(`tools.py:125`)은 장시간 게이트(live 전용)에서 같은 휴리스틱을 쓴다 — 본 스펙 미수정, 후속 가능 항목으로 문서에 한 줄 남김.

## 7. 테스트 전략 (TDD)

**C1**
- get_prices 의 `currency` 가 심볼모양과 다를 때 그 통화의 임계·버킷이 적용됨(예: 알파벳 심볼이지만 API 가 KRW → KRW 임계; `BRK.B` → USD).
- 조회 실패/빈 결과/통화 누락 → 심볼모양 폴백.
- MARKET+무금액 경로가 get_prices 를 한 번만 호출(ref price + currency 공유).

**M1**
- 업사이즈 델타가 일일캡에 검사되고 가산됨.
- 다운사이즈가 credit 되며 0 하한 적용.
- prev_notional 누락 시 전액 델타.
- 재부팅 후 `placed` + `modified` 델타까지 `restore_spend` 로 복원.
- 손상된 `modified` 이벤트 skip(부팅 무크래시).

**무회귀**
- `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q` 전체 그린.
- SDK 무회귀(`uv run --package pytossinvest --extra dev pytest pytossinvest/tests`) — 공개 API 불변.

## 8. 영향 파일 요약

| 파일 | 변경 |
|---|---|
| `tossinvest-mcp/src/tossinvest_mcp/safety.py` | `build_spec(currency=...)`, `OrderSpec.prev_notional`, `check_guardrails(prev_notional=...)` 델타 일일검사, `record_spend` 0 하한, `restore_spend` modified 합산+하한, `release` 제거 |
| `tossinvest-mcp/src/tossinvest_mcp/tools.py` | `preview_order`/`preview_modify` 통화 조회+주입, single get_prices 공유, preview_modify N_old 계산, modify_order `finalize(delta)`, 감사에 currency/delta |
| `tossinvest-mcp/tests/` | C1·M1 테스트 추가 |
| `CLAUDE.md`, `docs/claude/tossinvest-mcp.md` | 불변식·함정·통화/복원 절 갱신 |
