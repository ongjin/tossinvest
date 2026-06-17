> **언제 읽나**: `tossinvest-mcp`(MCP 서버) 코드를 만질 때 — 툴 추가/수정, 안전모델(모드·가드레일·preview/confirm·멱등성) 손보기, paper 엔진·감사로그 작업. 안전 불변식과 모듈별 책임의 living 레퍼런스. (외부 API 는 [tossinvest-open-api.md](tossinvest-open-api.md), SDK 는 [pytossinvest-sdk.md](pytossinvest-sdk.md), 설계 시점 기록은 `docs/superpowers/`.)
>
> **🔄 자가갱신**: MCP 코드를 바꾸면(새 툴·모드·가드레일·config·라우팅·함정) **같은 세션에 이 문서를 갱신**한다. 커밋은 수동. 코드가 진실 — 어긋나면 발견 즉시 고친다.

# tossinvest-mcp (MCP 서버) 내부구조

LLM(Claude Desktop/Cursor 등)에 토스 계좌 읽기/거래를 **안전하게** 쥐여주는 MCP 서버. **Apache-2.0**. `pytossinvest` SDK 의존. **stdio** 트랜스포트.

- 위치: `tossinvest-mcp/src/tossinvest_mcp/`
- 테스트: `uv run --package tossinvest-mcp pytest tossinvest-mcp/tests` (FakeClient + paper 엔진, 64개, **라이브 키 불필요**)
- 의존: `mcp`(FastMCP), `pydantic-settings`, `pytossinvest`.

## 🔒 안전 불변식 (이 프로젝트의 핵심 — 절대 깨지 말 것)

> **체결 경로(`paper.place` / `client.place_order`)는 반드시 `safety.check_guardrails` 를 거친다.** confirmation 토큰은 `preview_order` 에서만, 가드레일 통과 후 발급된다. `place_order` 는 `consume(token)` → 실행 → **성공 시에만 `finalize`**. 실패하면 토큰이 살아남아 **같은 `clientOrderId` 로 멱등 재시도**. 다른 `issue_token` 호출자나 가드레일 우회 체결 경로를 만들지 말 것.

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

deny심볼 → allow심볼 → **30억↑ 무조건 거부**(`max-order-exceeded`) → **1억↑ confirm 필수**(`confirm-high-value-required`) → 주문당 상한 → 일일 누적 상한 → 장시간(`enforce_hours` 일 때만). **순서가 테스트를 통과시키는 핵심**(상한 거대하게 두고 고액/30억 먼저 터지게) — 재배열 금지.
- 상수: `HIGH_VALUE_THRESHOLD=100,000,000`(`>=`), `MAX_ORDER_THRESHOLD=3,000,000,000`(`>`). notional 은 **주문통화 기준**(FX 환산 X).
- 장시간 게이트는 **live 전용** — `tools._market_gate` 가 `enforce = config.enforce_market_hours and app.is_live`. paper 는 아무때나 데모 가능.

## preview → place 토큰 생애 (`safety.py`)

- `build_spec(...)` — notional 계산(precedence: `order_amount` → `price*quantity` → `ref_price*quantity` → `GuardrailError("insufficient-order-params")`) + `clientOrderId` 자동 부여(`gen_id`).
- `issue_token(spec)` — `_pending[token]=_Pending(spec, expires_at=now+ttl)`. **`preview_order` 가 check_guardrails 통과 후에만 호출**.
- `consume(token)` — 존재·만료 검증 후 spec 반환. **pop 안 함**(만료면 삭제 후 `expired-confirmation`, 없으면 `invalid-confirmation`).
- `finalize(token, notional)` — `_pending.pop` + `record_spend`(일일 누적). **place 성공 시에만**.
- 멱등성: place 실패 시 finalize 안 함 → 토큰 살아있음 → 재시도가 같은 `clientOrderId` 재사용. 성공하면 pop 되어 2차 발사 불가.

## 13 툴 (`server.py` 등록, `tools.py` 구현)

- **읽기(항상)**: `get_accounts`·`get_holdings`·`get_quote`(단일심볼이면 orderbook+trades 동봉)·`get_candles`·`get_stock_info`·`get_market_info`(calendar + 옵션 FX)·`list_orders`·`get_order`
- **쓰기(read_only 외)**: `get_order_readiness`·`preview_order`→`place_order`·`modify_order`·`cancel_order`
- 출력 돈/수량은 전부 **문자열**(`_paper_order_dict`·holdings 등에서 `str()`). 툴 description 에 "string money / 2단계 주문 / live-only" 명시(LLM 가이드).

## 모듈별 함정 (이미 겪은 것)

- **paper modify/cancel 은 live 전용** — paper 즉시체결이라 미체결 주문 없음 → `PaperError`(실제 `409 already-filled` 미러링).
- **paper MARKET 무가격 체결 금지** — 체결 시점 `_ref_price` 가 None 이면 가격 0 으로 조용히 체결되던 버그 → `PaperError`(토큰 살림, 재시도 가능). US 금액주문 qty=amount/price 는 Decimal 나눗셈.
- **market_hours US 자정넘김** — 미국장 KST 표기는 23:30→06:00 처럼 wrap. `start>end` 면 `now>=start or now<end`. 깨진 시간 문자열은 "닫힘"(safe).
- **테스트 import** — `from conftest import FakeClient` (pytest 가 `tests/` 를 sys.path 에). `from tests.conftest` 는 `tests` 패키지 없어 깨짐.
- **call_tool 반환 형식 의존 금지** — MCP 버전마다 다름. 서버 테스트는 `list_tools()`(이름)로 검증, 동작은 `tools.py` 함수 직접 호출로 검증.

## 새 툴 추가 절차

1. `tools.py` 에 `fn(app, ...) -> dict` 추가. 계좌컨텍스트면 `if app.use_paper:` 분기(paper 엔진 vs `app.client`). 돈은 문자열 출력.
2. write 툴이면 안전 불변식 준수 — preview→place 패턴이면 `check_guardrails`/토큰 거치고, 감사로그 기록.
3. `server.py` 의 `_register_reads`/`_register_writes` 에 `@mcp.tool(name, description)` 클로저 추가 — `app` 캡처, `tools.fn(app, ...)` 위임. description 에 제약(문자열 머니 등) 명시.
4. 테스트: `tools.py` 함수 직접 호출(FakeClient) + 모드별 등록은 `test_server_modes`.
5. 이 문서 갱신.

## config (env `TOSSINVEST_`)

`mode`·`allow_live`·`client_id`·`client_secret`·`base_url`·`max_order_amount`(1,000,000)·`daily_order_limit`(5,000,000)·`allow_symbols`/`deny_symbols`(JSON 리스트)·`enforce_market_hours`(True)·`paper_starting_cash`(10,000,000)·`confirmation_ttl_sec`(120)·`audit_log_path`. 돈 필드는 `_no_float` validator 로 float 거부. 사용자용 표는 `tossinvest-mcp/README.md`.
