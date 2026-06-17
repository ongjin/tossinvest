> **언제 읽나**: `tossinvest-mcp`(MCP 서버) 코드를 만질 때 — 툴 추가/수정, 안전모델(모드·가드레일·preview/confirm·멱등성) 손보기, paper 엔진·감사로그 작업. 안전 불변식과 모듈별 책임의 living 레퍼런스. (외부 API 는 [tossinvest-open-api.md](tossinvest-open-api.md), SDK 는 [pytossinvest-sdk.md](pytossinvest-sdk.md), 설계 시점 기록은 `docs/superpowers/`.)
>
> **🔄 자가갱신**: MCP 코드를 바꾸면(새 툴·모드·가드레일·config·라우팅·함정) **같은 세션에 이 문서를 갱신**한다. 커밋은 수동. 코드가 진실 — 어긋나면 발견 즉시 고친다.

# tossinvest-mcp (MCP 서버) 내부구조

LLM(Claude Desktop/Cursor 등)에 토스 계좌 읽기/거래를 **안전하게** 쥐여주는 MCP 서버. **Apache-2.0**. `pytossinvest` SDK 의존. **stdio** 트랜스포트.

- 위치: `tossinvest-mcp/src/tossinvest_mcp/`
- 테스트: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests` (FakeClient + paper 엔진, 96개, **라이브 키 불필요**)
- 의존: `mcp`(FastMCP), `pydantic-settings`, `pytossinvest`.

## 🔒 안전 불변식 (이 프로젝트의 핵심 — 절대 깨지 말 것)

> **체결 경로(`paper.place` / `client.place_order`)는 반드시 `safety.check_guardrails` 를 거친다.** confirmation 토큰은 `preview_order` 에서만, 가드레일 통과 후 발급된다. `place_order` 는 `consume(token)` → 실행 → **성공 시에만 `finalize`**. 실패하면 토큰이 살아남아 **같은 `clientOrderId` 로 멱등 재시도**. **modify 도 동형**: `preview_modify`→`modify_order(confirmation_token)` — consume → 가드레일 재검사(`check_daily=False`) → 실행 → 성공 시 `release`(pop only, 일일 미가산) / 실패 시 토큰 유지. 다른 `issue_token` 호출자나 가드레일 우회 체결 경로를 만들지 말 것.

## 레이어 (의존 방향: 위 → 아래)

```
server.py     ← FastMCP, 모드별 툴 등록(read_only면 write 미등록), main() stdio 엔트리
tools.py      ← AppContext + 툴 함수 fn(app, ...). 라우팅(paper vs real). 테스트는 이 함수 직접 호출
  ├ safety.py       가드레일 + preview/confirm 토큰 + 멱등성 (pure, 시계 주입)
  ├ paper.py        시뮬 브로커 (즉시체결, Decimal)
  ├ market_hours.py 캘린더 → 개장/휴장 (pure)
  ├ audit.py        write 결정 JSONL 기록
  └ config.py       Settings (pydantic-settings, env TOSSINVEST_)
```
**설계 의도**: 무거운 로직은 pure 모듈(safety/paper/market_hours/audit)과 `tools.py` 함수에 두고 **직접 단위테스트**. `server.py` 는 얇은 glue — 모드별 **등록 여부만** `list_tools()` 로 검증(MCP 트랜스포트 내부에 의존 안 함).

## 3모드 (`TOSSINVEST_MODE`, 기본 `paper`)

| 모드 | write 툴 | 계좌읽기(accounts/holdings/orders) | 시세(quote/candles/stock/market) | 주문 |
|---|---|---|---|---|
| `read_only` | **미등록** | real client | real client | (불가) |
| **`paper`** | 등록 | **PaperBroker** | real client | **PaperBroker 체결** |
| `live` | 등록 | real client | real client | real client |

- `live` 는 `mode=live` **+ `TOSSINVEST_ALLOW_LIVE=1`** 둘 다 있어야 켜짐 — `config.py` 의 `model_validator` 가 둘째 없으면 `ValueError`(이중게이트, fail-closed).
- 라우팅 분기 키: `app.use_paper`(mode=="paper"), `app.is_live`(mode=="live"). **시세 툴은 모드 무관 항상 real**(시세는 계좌 무관).

## 가드레일 (`safety.check_guardrails`, 순서 중요)

`build_spec` 이 먼저 비양수 값(`quantity`·`price`·`order_amount` ≤ 0)을 `invalid-order-value` 로 거부. 그리고 `order_amount` 를 `price` 또는 `quantity` 와 동시 전달하면 `invalid-order-params` 로 거부(금액주문과 수량/가격 주문의 혼합 차단). 그다음 `check_guardrails` 가 아래 순서로 검사:

deny심볼 → allow심볼 → **하드실링 초과 무조건 거부**(`max-order-exceeded`) → **고액 + 미확인**(`confirm-high-value-required`) → 주문당 상한(`order-amount-cap`) → 일일 누적 상한(`daily-limit`, `check_daily=True` 일 때만) → 장시간(`enforce_hours` 일 때만, `market-closed`). **순서가 테스트를 통과시키는 핵심** — 재배열 금지.

**통화별 임계 및 상한** (심볼 모양으로 판정 — `order_currency(symbol)`):
| | KRW (숫자 심볼, 예 `005930`) | USD (영문 심볼, 예 `AAPL`) |
|---|---|---|
| 고액 confirm 임계 (`>=`) | `HIGH_VALUE_THRESHOLD` = 1억 | `HIGH_VALUE_THRESHOLD_USD` = $100,000 |
| 하드실링 (`>`) | `MAX_ORDER_THRESHOLD` = 30억 | `MAX_ORDER_THRESHOLD_USD` = $3,000,000 |
| 주문당 상한 | `config.max_order_amount` | `config.max_order_amount_usd` |
| 일일 누적 상한 | `config.daily_order_limit` | `config.daily_order_limit_usd` |

- **FX 환산 없음** — notional 은 주문통화 기준 비교. KRW/USD 버킷 분리(서로 막지 않음).
- **deny/allow 심볼 매칭은 정규화** — `check_guardrails` 에서 `spec.symbol` 과 리스트 양쪽을 `.strip().upper()` 로 정규화해 비교(대소문자·앞뒤 공백 무시). `spec.symbol` 자체는 변경 안 함 — 브로커에는 원본값이 그대로 전달된다.
- **`check_daily=False`** — modify(`preview_modify`→`modify_order`)는 주문당·고액·하드실링만 검사하고 일일 버킷엔 가산·검사 안 함(M1 설계).
- 장시간 게이트는 **live 전용** — `tools._market_gate` 가 `enforce = config.enforce_market_hours and app.is_live`. paper 는 아무때나 데모 가능.

## preview → place / preview_modify → modify 토큰 생애 (`safety.py`)

- `build_spec(...)` — 비양수 검증(`invalid-order-value`) → `order_amount` + `price`/`quantity` 동시 전달 거부(`invalid-order-params`) → notional 계산(precedence: `order_amount` → `price*quantity` → `ref_price*quantity` → `GuardrailError("insufficient-order-params")`) + `clientOrderId` 자동 부여(`gen_id`) + `currency=order_currency(symbol)` + `modify_order_id` 셋.
- `issue_token(spec)` — `_pending[token]=_Pending(spec, expires_at=now+ttl, issued_at=now)`. **`preview_order`·`preview_modify` 가 check_guardrails 통과 후에만 호출**.
- `consume(token)` — 존재·만료 검증 후 spec 반환. **pop 안 함**(만료면 삭제 후 `expired-confirmation`, 없으면 `invalid-confirmation`). **live 최소지연 게이트**: `config.live_confirm_min_delay_sec > 0` 이고 `config.is_live` 이면 `now - issued_at < delay` 일 때 `confirm-too-soon`.
- `finalize(token, notional)` — `_pending.pop` + `record_spend(notional, currency)`(통화별 일일 누적). **place 성공 시에만**.
- `release(token)` — `_pending.pop` 만. 일일 누적 가산 없음. **modify 성공 시에만** (M1 — 정정은 일일 버킷 미가산).
- `record_spend(notional, currency)` — `_roll_daily()` 후 `_spent[currency]` 에 가산. KRW/USD 버킷 분리.
- `restore_spend(events)` — 부팅 시 audit `read_events()` 결과를 받아 당일(`ts` UTC → KST 날짜 변환) `placed` 이벤트의 `notional`·`currency` 를 `_spent` 에 복원. dict 가 아닌 이벤트, `notional`/`ts` 누락, 파싱 불가 값은 건너뜀(손상 감사 파일이 있어도 부팅 불가 없음). 감사 파일이 없거나 지워지면 복원 누락(누적 0으로 리셋됨 — 주의).
- 멱등성: place 실패 시 finalize 안 함 → 토큰 살아있음 → 재시도가 같은 `clientOrderId` 재사용. 성공하면 pop 되어 2차 발사 불가. modify 실패 시 release 안 함 → 같은 패턴.
- **place 시 일일한도 재검사**: `place_order` 는 `consume` 직후 실행 전에 `check_guardrails(spec, ..., check_daily=True)` 를 다시 호출 — preview 를 여러 개 발급해 한도를 초과하는 우회 차단.

## 14 툴 (`server.py` 등록, `tools.py` 구현)

- **읽기(항상)**: `get_accounts`·`get_holdings`·`get_quote`(단일심볼이면 orderbook+trades 동봉)·`get_candles`·`get_stock_info`·`get_market_info`(calendar + 옵션 FX)·`list_orders`·`get_order`
- **쓰기(read_only 외)**: `get_order_readiness`·`preview_order`→`place_order`·**`preview_modify`**→`modify_order(confirmation_token)`·`cancel_order`
  - `preview_modify(order_id, order_type, price=None, quantity=None, confirm_high_value_order=False)` — live 전용. 원주문 조회 → 병합 → `build_spec(modify_order_id=order_id)` → `check_guardrails(check_daily=False)` → `issue_token` → 감사(`modify_previewed`, previousStatus).
  - `modify_order(confirmation_token)` — live 전용. `consume` → `check_guardrails(check_daily=False)` 재검사 → `client.modify_order` → `release`(성공 시) / 토큰 유지(실패 시) → 감사(`modified`).
  - `cancel_order(order_id)` — live 전용. 취소 전 원주문 `previousStatus` 를 감사(`canceled`)에 기록.
- 출력 돈/수량은 전부 **문자열**(`_paper_order_dict`·holdings 등에서 `str()`). 툴 description 에 "string money / 2단계 주문 / live-only" 명시(LLM 가이드).

## 모듈별 함정 (이미 겪은 것)

- **paper modify/cancel 은 live 전용** — paper 즉시체결이라 미체결 주문 없음 → `PaperError`(실제 `409 already-filled` 미러링).
- **paper MARKET 무가격 체결 금지** — 체결 시점 `_ref_price` 가 None 이면 가격 0 으로 조용히 체결되던 버그 → `PaperError`(토큰 살림, 재시도 가능). US 금액주문 qty=amount/price 는 Decimal 나눗셈.
- **market_hours US 자정넘김** — 미국장 KST 표기는 23:30→06:00 처럼 wrap. `start>end` 면 `now>=start or now<end`. 깨진 시간 문자열은 "닫힘"(safe).
- **테스트 import** — `from conftest import FakeClient` (pytest 가 `tests/` 를 sys.path 에). `from tests.conftest` 는 `tests` 패키지 없어 깨짐.
- **call_tool 반환 형식 의존 금지** — MCP 버전마다 다름. 서버 테스트는 `list_tools()`(이름)로 검증, 동작은 `tools.py` 함수 직접 호출로 검증.
- **통화 판정은 심볼 모양** — `order_currency(symbol)`: `symbol.isalpha()` 이면 USD, 아니면 KRW. `AAPL`→USD, `005930`→KRW. FX 환산 없음 — 안전 상한이 환율·네트워크에 의존하지 않게. KRW/USD `_spent` 버킷이 분리돼 한 통화 한도가 다른 통화를 막지 않는다. **[C1 알려진 한계]** 점/접미사 포함 US 티커(`BRK.B` 등)와 공백 변형은 `isalpha()` 가 `False` 를 반환해 KRW 로 판정 → KRW 임계 적용. 회귀 아님(브랜치 이전도 전부 KRW). 정확한 통화는 `get_stocks`/`get_prices` 의 `currency` 기반 후속 PR 과제; 외부 의존 0 결정은 유지.
- **M1 modify 일일누적 미가산** — `preview_modify`·`modify_order` 는 `check_daily=False` 로 호출 — 정정 후 금액에 주문당·고액·하드실링·allow-deny 만 검사, 일일 버킷엔 가산·검사 안 함. 델타 회계 없음(정정이 전체 notional 을 다시 계산하는 구조이므로 의도적 설계).
- **부팅 복원(UTC ts → KST 날짜)** — `restore_spend` 는 감사 이벤트의 `ts`(UTC ISO) 를 `datetime.fromisoformat(ts).astimezone(_KST).date()` 로 변환해 오늘 KST 날짜와 비교. 파싱 실패는 건너뜀. dict 가 아닌 이벤트나 `notional`/`ts` 필드 누락 이벤트도 건너뜀(손상 감사 파일 있어도 서버 부팅 불가 없음). 감사 파일을 지우면 당일 누적도 0으로 리셋된다(주의).
- **`invalid-order-value` (양수 검증)** — `build_spec` 에서 `quantity`·`price`·`order_amount` 가 전달된 경우 `<= 0` 이면 `GuardrailError("invalid-order-value")`. notional 이 음수여서 상한 게이트를 조용히 통과하던 구멍 차단.

## 새 툴 추가 절차

1. `tools.py` 에 `fn(app, ...) -> dict` 추가. 계좌컨텍스트면 `if app.use_paper:` 분기(paper 엔진 vs `app.client`). 돈은 문자열 출력.
2. write 툴이면 안전 불변식 준수 — preview→place 패턴(또는 preview_modify→modify 패턴)이면 `check_guardrails`/토큰 거치고, 감사로그 기록. `check_daily` 플래그를 올바르게(place=True, modify=False).
3. `server.py` 의 `_register_reads`/`_register_writes` 에 `@mcp.tool(name, description)` 클로저 추가 — `app` 캡처, `tools.fn(app, ...)` 위임. description 에 제약(문자열 머니 / live-only 등) 명시.
4. 테스트: `tools.py` 함수 직접 호출(FakeClient) + 모드별 등록은 `test_server_modes`.
5. 이 문서 갱신(가드레일 순서·툴 수·함정).

## config (env `TOSSINVEST_`)

`mode`·`allow_live`·`client_id`·`client_secret`·`base_url`·`max_order_amount`(1,000,000 KRW)·`daily_order_limit`(5,000,000 KRW)·`max_order_amount_usd`(1,000 USD)·`daily_order_limit_usd`(5,000 USD)·`allow_symbols`/`deny_symbols`(JSON 리스트)·`enforce_market_hours`(True)·`paper_starting_cash`(10,000,000)·`confirmation_ttl_sec`(120)·`live_confirm_min_delay_sec`(0, off — live 환경 권장 5)·`audit_log_path`. 돈 필드(`max_order_amount`·`daily_order_limit`·`max_order_amount_usd`·`daily_order_limit_usd`·`paper_starting_cash`)는 `_no_float` validator 로 float 거부. 사용자용 표는 `tossinvest-mcp/README.md`.
