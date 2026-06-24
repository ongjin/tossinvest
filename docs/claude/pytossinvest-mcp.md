> **언제 읽나**: `pytossinvest-mcp`(MCP 서버) 코드를 만질 때 — 툴 추가/수정, 안전모델(모드·가드레일·preview/confirm·멱등성) 손보기, paper 엔진·감사로그 작업. 안전 불변식과 모듈별 책임의 living 레퍼런스. (외부 API 는 [tossinvest-open-api.md](tossinvest-open-api.md), SDK 는 [pytossinvest-sdk.md](pytossinvest-sdk.md), 설계 시점 기록은 `docs/superpowers/`.)
>
> **🔄 자가갱신**: MCP 코드를 바꾸면(새 툴·모드·가드레일·config·라우팅·함정) **같은 세션에 이 문서를 갱신**한다. 커밋은 수동. 코드가 진실 — 어긋나면 발견 즉시 고친다.

# pytossinvest-mcp (MCP 서버) 내부구조

LLM(Claude Desktop/Cursor 등)에 토스 계좌 읽기/거래를 **안전하게** 쥐여주는 MCP 서버. **Apache-2.0**. `pytossinvest` SDK 의존. **stdio**(기본) 또는 **http** 트랜스포트.

- 위치: `pytossinvest-mcp/src/pytossinvest_mcp/`
- 테스트: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests` (FakeClient + paper 엔진, 179개, **라이브 키 불필요**)
- 의존: `mcp`(FastMCP), `pydantic-settings`, `pytossinvest`. 옵션 extra: `redis = ["redis>=5"]`(HA 백엔드), `http = ["uvicorn>=0.30"]`(http 트랜스포트). dev extra: `fakeredis[lua]>=2`(테스트).

## 🔒 안전 불변식 (이 프로젝트의 핵심 — 절대 깨지 말 것)

> **체결 경로(`paper.place` / `client.place_order`)는 반드시 `safety.check_guardrails` 를 거친다.** confirmation 토큰은 `preview_order` 에서만, 가드레일 통과 후 발급된다. `place_order` 는 `consume(token)` → `check_guardrails(check_daily=False)` → **`reserve`(원자적·`clientOrderId` 멱등)** → 실행 → **성공 시 `commit`(일일 누적 확정) / 실패 시 `release`(예약 해제)** → 실패 시 토큰 유지, 동일 `clientOrderId` 로 멱등 재시도. **modify 도 동형**: `preview_modify`→`modify_order(confirmation_token)` — consume → 가드레일 재검사(**델타 회계** — `check_daily=True, prev_notional=원본명목`) → **`reserve(signed delta)`** → 실행 → 성공 시 **`commit(delta)`**(pop + 델타 가산, 0-하한) / 실패 시 **`release(delta)`**(예약 해제, 토큰 유지). 다른 `issue_token` 호출자나 가드레일 우회 체결 경로를 만들지 말 것.

## 레이어 (의존 방향: 위 → 아래)

```
server.py     ← FastMCP build_server(stateless_http 분기), run_server(stdio|http 분기), main()
http.py       ← ASGI 조립 + bearer 미들웨어 (BearerAuthMiddleware / build_http_app / serve_http)
tools.py      ← AppContext + 툴 함수 fn(app, ...). 라우팅(paper vs real). 테스트는 이 함수 직접 호출
  ├ safety.py       가드레일 + preview/confirm 토큰 + 멱등성 (pure, 시계 주입)
  ├ paper.py        시뮬 브로커 (즉시체결, Decimal)
  ├ market_hours.py 캘린더 → 개장/휴장 (pure)
  ├ audit.py        write 결정 JSONL 기록
  └ config.py       Settings (pydantic-settings, env TOSSINVEST_)
```
**설계 의도**: 무거운 로직은 pure 모듈(safety/paper/market_hours/audit)과 `tools.py` 함수에 두고 **직접 단위테스트**. `server.py` 는 얇은 glue — 모드별 **등록 여부만** `list_tools()` 로 검증(MCP 트랜스포트 내부에 의존 안 함). `http.py` 는 트랜스포트 glue — stdio 설치에는 임포트되지 않는다.

## 3모드 (`TOSSINVEST_MODE`, 기본 `paper`)

| 모드 | write 툴 | 계좌읽기(accounts/holdings/orders) | 시세(quote/candles/stock/market) | 주문 |
|---|---|---|---|---|
| `read_only` | **미등록** | real client | real client | (불가) |
| **`paper`** | 등록 | **PaperBroker** | real client | **PaperBroker 체결** |
| `live` | 등록 | real client | real client | real client |

- `live` 는 `mode=live` **+ `TOSSINVEST_ALLOW_LIVE=1`** 둘 다 있어야 켜짐 — `config.py` 의 `model_validator` 가 둘째 없으면 `ValueError`(이중게이트, fail-closed).
- 라우팅 분기 키: `app.use_paper`(mode=="paper"), `app.is_live`(mode=="live"). **시세 툴은 모드 무관 항상 real**(시세는 계좌 무관).

## 트랜스포트 (`TOSSINVEST_TRANSPORT`, 기본 `stdio`)

| 값 | 동작 | 엔트리 |
|---|---|---|
| `stdio`(기본) | `mcp.run()` — Claude Desktop/Cursor 등 MCP 클라이언트용, 무변경 | `run_server` stdio 분기 |
| `http` | Starlette ASGI `/mcp`(Streamable HTTP) + `BearerAuthMiddleware` + uvicorn | `run_server` http 분기 |

- **`build_server`**: `stateless_http=(settings.transport == "http")` 로 FastMCP 초기화. stdio 일 때는 `stateless_http=False`.
- **`run_server(settings, mcp)`**: http 면 `build_http_app(mcp, auth_token=settings.auth_token)` → `serve_http(app, host, port)`. stdio 면 `mcp.run()`.
- **`http.py`**: `BearerAuthMiddleware(BaseHTTPMiddleware)` — `hmac.compare_digest` 상수시간 bearer 검증, 실패 시 401. `build_http_app(mcp, *, auth_token)` — `mcp.streamable_http_app()` 에 미들웨어 추가 후 Starlette 앱 반환. `serve_http(app, *, host, port)` — uvicorn runner(함수 내부에서만 `import uvicorn` — `[http]` extra).
- **직교성**: transport 축은 `MODE`(read_only/paper/live) 및 `STATE_BACKEND`(memory/redis)와 독립. 어떤 조합이든 동작. 다중 인스턴스 HA = `http` + `redis` 백엔드 조합 권장.
- **Phase 2(인스턴스 간 공유 레이트리미터·OAuth 토큰캐시)는 의도적 보류 (2026-06-19 결정, YAGNI)** — redis 백엔드가 공유하는 건 **spend 카운터·paper 상태·confirmation 토큰**까지다. **SDK 의 레이트리밋(`ratelimit.TokenBucket`)과 OAuth 토큰(`auth.TokenManager`)은 여전히 프로세스 로컬**이라, 다중 인스턴스를 동시에 띄우면 각자 별도 버킷/토큰을 갖는다(토스 per-account 한도는 인스턴스 간 미공유 — 초과 시 `X-RateLimit-*` 헤더동기화 + 429 bounded retry 로 수습). 단일테넌트(유저 1명)·저부하라 **1-인스턴스로 충분**하므로 공유 토큰버킷은 만들지 않았다. 진짜로 여러 인스턴스를 LB 뒤에서 동시 구동해 한도를 공유해야 하는 실수요가 생기면 그때 SDK 에 seam 을 뚫는다(설계 시점 기록: `docs/superpowers/specs/2026-06-18-self-host-remote-mcp-design.md §8` Phase 2).

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
- **modify 델타 회계** — modify(`preview_modify`→`modify_order`)는 `check_daily=True, prev_notional=원본명목`으로 호출 — 일일 버킷은 증분(`new−old`)만 검사·가산. per-order·고액·하드실링은 전액으로 검사. `reserve(signed delta)` → 성공 시 `commit` / 실패 시 `release` 로 0-하한(다운사이즈 시 credit, 음수 방지).
- 장시간 게이트는 **live 전용** — `tools._market_gate(app, symbol, currency)` 가 `enforce = config.enforce_market_hours and app.is_live`. paper 는 아무때나 데모 가능. **국가 판정은 권위 통화 우선** — `_country_for_order(symbol, currency)`: `currency` 가 `USD`→`US`·`KRW`→`KR`(정규화 후), 없으면 `symbol.isalpha()` 심볼모양 폴백. `preview_order`·`preview_modify` 가 `spec.currency`(C1 권위 통화)를 넘겨 가드레일 통화와 장시간 게이트 국가가 한 소스로 일치(`BRK.B` 처럼 `isalpha()` 가 어긋나는 티커도 API 통화가 있으면 정확).

## preview → place / preview_modify → modify 토큰 생애 (`safety.py`)

- `build_spec(...)` — 비양수 검증(`invalid-order-value`) → `order_amount` + `price`/`quantity` 동시 전달 거부(`invalid-order-params`) → notional 계산(precedence: `order_amount` → `price*quantity` → `ref_price*quantity` → `GuardrailError("insufficient-order-params")`) + `clientOrderId` 자동 부여(`gen_id`) + `currency` 파라미터 우선, 없으면 `order_currency(symbol)` 폴백 + `modify_order_id` 셋. **`prev_notional` 은 `build_spec` 인자가 아니다** — `preview_modify` 가 `build_spec` 호출 후 `spec.prev_notional`(원본 price×qty)에 직접 대입한다. `tools.py` 의 `preview_order`·`preview_modify` 는 `_price_and_currency(app, symbol)` 로 `get_prices` 한 번 → 권위 통화를 주입; 조회 실패/공백 시 폴백.
- `issue_token(spec)` — token_store 에 `(spec, expires_at=now+ttl, issued_at=now)` 저장(TTL 포함). **`preview_order`·`preview_modify` 가 check_guardrails 통과 후에만 호출**.
- `consume(token)` — 존재·만료 검증 후 spec 반환. **pop 안 함**(만료면 삭제 후 `expired-confirmation`, 없으면 `invalid-confirmation`). **live 최소지연 게이트**: `config.live_confirm_min_delay_sec > 0` 이고 `config.is_live` 이면 `now - issued_at < delay` 일 때 `confirm-too-soon`.
- **reserve-first place 흐름**: `place_order` 는 `consume` 후 `check_guardrails(check_daily=False)` → `store.reserve(day, currency, notional, cap, clientOrderId)` → 실행 → 성공 시 `store.commit(token)` / 실패 시 `store.release(day, currency, notional, clientOrderId)`. `reserve` 는 원자적(`clientOrderId` 멱등 — 같은 키로 중복 예약 시 기존 결과 반환). 실패해도 토큰은 살아있어 재시도 가능.
- **reserve-first modify 흐름**: `modify_order` 는 `consume` 후 `check_guardrails(check_daily=True, prev_notional=spec.prev_notional)` → `store.reserve(day, currency, delta, cap, clientOrderId)` → 실행 → 성공 시 `store.commit(token)` / 실패 시 `store.release(day, currency, delta, clientOrderId)`. delta = new−old(부호있는 델타).
- `restore_spend(events)` — **memory 백엔드 전용**. 부팅 시 audit `read_events()` 결과를 받아 당일(`ts` UTC → KST 날짜 변환) `placed`/`modified` 이벤트의 `notional`·`currency` 를 SpendStore 에 `seed` 로 합산(통화별 0-하한). dict 가 아닌 이벤트, `notional`/`ts` 누락, 파싱 불가 값은 건너뜀(손상 감사 파일이 있어도 부팅 불가 없음). 감사 파일이 없거나 지워지면 복원 누락(누적 0으로 리셋됨 — 주의). **redis 백엔드에서는 `seed` 가 no-op** — counter 가 AOF로 지속, 재시드 시 이중 계산 방지.
- 멱등성: place/modify 실패 시 `release` 호출, 토큰 유지 → 동일 `clientOrderId` 로 멱등 재시도. `commit` 후에는 토큰 pop 되어 2차 발사 불가.
- **place 시 일일한도 재검사**: `place_order` 는 `consume` 직후 실행 전에 `check_guardrails(spec, ..., check_daily=False)` → `reserve` 가 원자적으로 cap 검사 — preview 를 여러 개 발급해 한도를 초과하는 우회 차단.

## 상태 백엔드 (memory | redis)

`safety.py` 는 `TokenStore` / `SpendStore` 두 인터페이스를 통해 상태를 읽고 쓴다. 백엔드는 `TOSSINVEST_STATE_BACKEND`(`memory`기본 / `redis`) 환경변수로 선택된다.

### TokenStore 인터페이스

| 메서드 | 설명 |
|---|---|
| `issue_token(spec) -> str` | pending 맵에 토큰 발급(TTL 포함) |
| `consume(token) -> OrderSpec` | 존재·만료 검증 후 spec 반환(pop 안 함) |
| `commit(token) -> None` | 성공 확정 — pending 맵에서 pop |
| `release_token(token) -> None` | 실패 시 토큰 유지(no-op, 명시적 인터페이스) |

### SpendStore 인터페이스

| 메서드 | 설명 |
|---|---|
| `reserve(day, currency, delta, cap, dedup_key) -> bool` | 원자적 예약 — 한도 초과 시 False, `dedup_key`(`clientOrderId`) 기반 멱등 |
| `release(day, currency, delta, dedup_key) -> None` | 실패 시 예약 해제 |
| `commit(token) -> None` | 성공 확정 — 예약을 영구 기록으로 전환 |
| `seed(currency, amount) -> None` | 부팅 복원용 — memory: 직접 주입; **redis: no-op** |

### memory 백엔드 (`stores.py`)

기본값. 단일 인스턴스, 재시작 시 휘발. 부팅 시 `restore_spend(audit_events)` 로 당일 누적 복원(`placed`+`modified` 이벤트 합산).

### redis 백엔드 (`redis_stores.py`)

`redis>=5` + `fakeredis[lua]>=2`(dev/test). `TOSSINVEST_REDIS_URL` 필요.

- **SpendStore**: day-scope key(`spend:{day}:{currency}`)에 Decimal 문자열 저장. `reserve`/`release` 모두 `lock:spend:{day}` Lock(day-scope) + Python Decimal RMW. **`INCR`/`INCRBYFLOAT` 금지** — 돈은 float 불가, Decimal 직렬화만.
- **dedup set**: `reserved:{day}` Set에 `clientOrderId` 저장으로 중복 예약 방지(멱등성). day-scoped 만으로 충분 — `clientOrderId` 가 전역적으로 고유하므로 통화를 키에 포함할 필요 없음.
- **`seed` no-op**: redis counter 가 진실의 원천(AOF 지속). 감사 파일을 지워도 redis 카운터는 유지.
- **Lock.release()**: redis-py 내부적으로 EVALSHA(Lua) 사용 → 테스트에서 `fakeredis[lua]` 서브패키지 필수(없으면 `ResponseError`). 실제 Redis 는 Lua 기본 내장.

### RedisAuditSink (`audit.py`)

감사 이벤트를 Redis Stream(`audit`)에 기록. JSONL 파일 감사와 병렬 사용 가능. `read_events()` 는 Stream에서 전체 이벤트 조회.

### fail-closed 래퍼 (`_guard_store`)

store I/O (`reserve`/`release`/`commit`/`seed`) 중 `ConnectionError`/`Timeout`/`OSError` 발생 시 → `GuardrailError("state-unavailable")`로 변환(fail-closed). `GuardrailError` 자체는 절대 삼키지 않는다 — 항상 호출자에게 전파.

### PaperStore 심 (`paper.py` + `redis_stores.py`)

`PaperBroker`는 `PaperStore` 인터페이스(`lock() → context manager`, `load() → PaperState`, `save(state)`)를 통해 상태를 읽고 쓴다. 백엔드는 `state_backend` 설정값에 따라 `server.py` 의 `_build_stores`(4-tuple 반환)에서 선택된다.

**`MemoryPaperStore`** (기본, `paper.py`):
- 인스턴스 로컬 — 재시작 시 휘발. `lock()` 은 `nullcontext`(단일 스레드 가정).

**`RedisPaperStore`** (`redis_stores.py`):
- `state_backend=="redis"` 일 때 선택 — `TokenStore`/`SpendStore`와 동일한 redis client 공유.
- 단일 JSON 키 **`paper`** 에 `PaperState` 전체를 직렬화(읽기 `GET paper`, 쓰기 `SET paper`).
- 뮤테이션은 redis-py `Lock(f"lock:{key}")` = **`lock:paper`** 락 아래에서만. `PaperBroker.place()` 의 `clientOrderId` 멱등 체크도 이 락 안에서 수행(기존 주문 있으면 두 번째 체결 없이 반환).
- **다중 인스턴스 공유·재시작 생존** — AOF/RDB 지속 redis라면 재시작 후 paper 상태 복원.

**`PaperState` 필드**: `cash`(dict[str, Decimal] — 통화→잔고)·`positions`(symbol→Position)·`orders`(list[PaperOrder])·`realized_pnl`(dict[str, Decimal] — 통화→실현손익)·`counter`(int). **`Position`** 은 `quantity`, `avg_price`, `currency`(str) 세 필드를 가진다.

**통화 버킷 분리**: `cash`/`realized_pnl` 는 `{"KRW": ..., "USD": ...}` 형태로 통화별로 완전히 분리된다(FX 환산 없음). BUY 는 해당 통화 버킷에서만 차감; SELL 은 `Position.currency`(매수 시 태그된 통화)를 권위값으로 사용한다. 없는 통화 버킷으로 매수 시 `PaperError("insufficient {currency} cash: ...")`. `buying_power(currency)` 는 해당 통화 버킷 반환(없으면 `Decimal("0")`).

**starting_cash**: `MemoryPaperStore(starting_cash=...)` 와 `RedisPaperStore(..., starting_cash=...)` 는 scalar(`"10000000"` → `{"KRW": ...}`) 또는 dict(`{"KRW": "10000000", "USD": "1000000"}`) 모두 받는다. 내부 정규화 함수 `_as_cash_dict(v)` 가 scalar→KRW dict 변환을 담당.

**직렬화 규칙**: 모든 금액/수량(`cash` dict values, `realized_pnl` dict values, `quantity`, `avg_price`, `price`) = Decimal 문자열(`str()`로 쓰기, `to_decimal()`로 읽기). **float 금지**. `Position.currency` 도 직렬화에 포함.

**레거시 마이그레이션**: `_paper_state_from_dict` 가 구 포맷(scalar `cash`, scalar `realized_pnl`, position 에 `currency` 없음)을 감지해 자동 업그레이드 — scalar → `{"KRW": ...}`, position 통화 없으면 `isalpha()` 심볼모양 폴백(알파→USD, 숫자→KRW).

### 한계 (현재 단계)

transport는 `stdio`(기본, 단일 클라이언트) 또는 `http`(원격 다중 클라이언트 가능). 다중 인스턴스 redis paper 공유는 가능하나 동시 paper 충돌 시 lock 대기 시간(기본 5s)이 생길 수 있다. 다중 인스턴스 HA = `http` + `redis` 백엔드 조합.

## 14 툴 (`server.py` 등록, `tools.py` 구현)

- **읽기(항상)**: `get_accounts`·`get_holdings`·`get_quote`(단일심볼이면 orderbook+trades 동봉)·`get_candles`·`get_stock_info`·`get_market_info`(calendar + 옵션 FX)·`list_orders`·`get_order`
- **쓰기(read_only 외)**: `get_order_readiness`·`preview_order`→`place_order`·**`preview_modify`**→`modify_order(confirmation_token)`·`cancel_order`
  - `preview_modify(order_id, order_type, price=None, quantity=None, confirm_high_value_order=False)` — live 전용. 원주문 조회 → 병합 → `build_spec(modify_order_id=order_id, prev_notional=원본price×qty, currency=권위통화)` → `check_guardrails(check_daily=True, prev_notional=…)` → `issue_token` → 감사(`modify_previewed`, previousStatus).
  - `modify_order(confirmation_token)` — live 전용. `consume` → `check_guardrails(check_daily=True, prev_notional=spec.prev_notional)` 재검사 → `reserve(signed delta)` → `client.modify_order` → 성공 시 `commit(token)`(delta 영구 반영, 0-하한) / 실패 시 `release(delta)`, 토큰 유지 → 감사(`modified`, notional=delta, currency).
  - `cancel_order(order_id)` — live 전용. 취소 전 원주문 `previousStatus` 를 감사(`canceled`)에 기록.
- 출력 돈/수량은 전부 **문자열**(`_paper_order_dict`·holdings 등에서 `str()`). 툴 description 에 "string money / 2단계 주문 / live-only" 명시(LLM 가이드).

## 모듈별 함정 (이미 겪은 것)

- **paper modify/cancel 은 live 전용** — paper 즉시체결이라 미체결 주문 없음 → `PaperError`(실제 `409 already-filled` 미러링).
- **paper MARKET 무가격 체결 금지** — 체결 시점 `_ref_price` 가 None 이면 가격 0 으로 조용히 체결되던 버그 → `PaperError`(토큰 살림, 재시도 가능). US 금액주문 qty=amount/price 는 Decimal 나눗셈.
- **market_hours US 자정넘김** — 미국장 KST 표기는 23:30→06:00 처럼 wrap. `start>end` 면 `now>=start or now<end`. 깨진 시간 문자열은 "닫힘"(safe).
- **테스트 import** — `from conftest import FakeClient` (pytest 가 `tests/` 를 sys.path 에). `from tests.conftest` 는 `tests` 패키지 없어 깨짐.
- **call_tool 반환 형식 의존 금지** — MCP 버전마다 다름. 서버 테스트는 `list_tools()`(이름)로 검증, 동작은 `tools.py` 함수 직접 호출로 검증.
- **통화 판정 — 권위 통화 + 폴백(C1)** — `preview_order`/`preview_modify` 는 `_price_and_currency(app, symbol)` 로 `get_prices([symbol])` 한 번을 호출해 `Price.currency`(권위 통화)를 얻고 `build_spec(currency=…)` 로 주입. 조회 실패·빈 결과·공백 통화면 `order_currency(symbol)` 폴백(`isalpha()`→USD, 아니면 KRW). `BRK.B` 등도 API 통화가 있으면 정확. `order_currency` 자체는 폴백 경로로만 남음. FX 환산 없음. KRW/USD 버킷 분리 유지. **이 권위 통화는 장시간 게이트 국가 판정에도 재사용**(`_market_gate`→`_country_for_order`) — 가드레일 통화와 미장/한국장 판정이 동일 소스. 통화가 없을 때만 `_country_for_order` 가 `isalpha()` 심볼모양으로 폴백.
- **M1 modify 델타 회계** — `preview_modify`·`modify_order` 는 `check_daily=True, prev_notional=원본명목` 으로 호출 — 일일 버킷은 증분(`new−old`)만 검사·가산, per-order·고액·하드실링은 전액 검사. 성공 시 `commit(token)`, SpendStore 가 0-하한. 다운사이즈(delta<0)는 credit 되어 한도가 느슨해질 수 있음(0-하한으로 음수 방지). 부팅복원도 `placed`+`modified` 델타 합산 후 0-하한.
- **부팅 복원(UTC ts → KST 날짜)** — `restore_spend` 는 감사 이벤트의 `ts`(UTC ISO) 를 `datetime.fromisoformat(ts).astimezone(_KST).date()` 로 변환해 오늘 KST 날짜와 비교. `placed` 와 `modified` 이벤트의 `notional`·`currency` 를 합산한 뒤 통화별 0-하한 적용. 파싱 실패·dict 가 아닌 이벤트·`notional`/`ts` 필드 누락은 건너뜀(손상 감사 파일 있어도 서버 부팅 불가 없음). 감사 파일을 지우면 당일 누적도 0으로 리셋된다(주의).
- **`invalid-order-value` (양수 검증)** — `build_spec` 에서 `quantity`·`price`·`order_amount` 가 전달된 경우 `<= 0` 이면 `GuardrailError("invalid-order-value")`. notional 이 음수여서 상한 게이트를 조용히 통과하던 구멍 차단.
- **http 트랜스포트 인증** — `TOSSINVEST_TRANSPORT=http` 로 부팅 시 `TOSSINVEST_AUTH_TOKEN` 이 비어있으면 `_http_requires_auth_token` model_validator 가 `ValueError` 발생(live/redis 와 동형 삼중 fail-closed). bearer 는 `hmac.compare_digest` 상수시간 비교 — 타이밍 공격 방지. 토큰은 엔드포인트 인증 전용, 단일 테넌트 — Redis 미저장, Toss API 자격증명 아님. `serve_http` 는 `uvicorn` 을 함수 내부에서만 import → stdio 설치 및 테스트 스위트는 uvicorn 없이 동작.
- **http 모드 DNS 리바인딩 보호 비활성화** — `build_server` 는 `transport=="http"` 일 때 `FastMCP(..., transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))` 를 전달한다. FastMCP 기본값은 host가 localhost 계열이면 DNS 리바인딩 보호를 자동 활성화해 비-localhost `Host` 헤더를 **421**으로 거부하는데, 원격 배포(Docker/리버스프록시)에서는 `mcp.example.com` 등 외부 호스트 헤더를 쓰므로 모든 요청이 421 처리된다. 인증 표면은 bearer 미들웨어(`BearerAuthMiddleware`)이고 배포 host 는 운영자/프록시가 제어하므로 Host 헤더 기반 방어가 불필요 — 의도적으로 비활성화. **stdio 경로는 이 인자를 전달하지 않아 기존 그대로.** 단, 운영자가 자기 host 를 알면 `TOSSINVEST_HTTP_ALLOWED_HOSTS`(JSON 리스트, 기본 `[]`)로 **opt-in Host 핀닝**을 켤 수 있다 — 비면 위처럼 보호 off, 비어있지 않으면 `enable_dns_rebinding_protection=True` + `allowed_hosts=<목록>` 으로 전달해 목록에 없는 `Host`→421(심화방어). 매칭은 정확일치 + `host:*` 포트 와일드카드(mcp `TransportSecurityMiddleware`). 빈 문자열 env 값은 JSON 파싱 실패로 부팅 거부되므로 deploy 템플릿은 해당 env 를 주석 처리(설정 시에만 비-빈 JSON 리스트).

## 새 툴 추가 절차

1. `tools.py` 에 `fn(app, ...) -> dict` 추가. 계좌컨텍스트면 `if app.use_paper:` 분기(paper 엔진 vs `app.client`). 돈은 문자열 출력.
2. write 툴이면 안전 불변식 준수 — preview→place 패턴(또는 preview_modify→modify 패턴)이면 `check_guardrails`/토큰 거치고, 감사로그 기록. `check_daily` 플래그를 올바르게(place=True, modify=True+prev_notional=원본명목).
3. `server.py` 의 `_register_reads`/`_register_writes` 에 `@mcp.tool(name, description)` 클로저 추가 — `app` 캡처, `tools.fn(app, ...)` 위임. description 에 제약(문자열 머니 / live-only 등) 명시.
4. 테스트: `tools.py` 함수 직접 호출(FakeClient) + 모드별 등록은 `test_server_modes`.
5. 이 문서 갱신(가드레일 순서·툴 수·함정).

## config (env `TOSSINVEST_`)

`mode`·`allow_live`·`client_id`·`client_secret`·`base_url`·`max_order_amount`(1,000,000 KRW)·`daily_order_limit`(5,000,000 KRW)·`max_order_amount_usd`(1,000 USD)·`daily_order_limit_usd`(5,000 USD)·`allow_symbols`/`deny_symbols`(JSON 리스트)·`enforce_market_hours`(True)·`paper_starting_cash`(**`{"KRW":"10000000"}` — 통화별 JSON dict**; 스칼라는 `{"KRW": …}` 래핑; float·bool 거부)·`confirmation_ttl_sec`(120)·`live_confirm_min_delay_sec`(0, off — live 환경 권장 5)·`audit_log_path`·**`state_backend`**(`memory`기본/`redis`)·**`redis_url`**(redis 백엔드 필수)·**`transport`**(`stdio`기본/`http`)·**`http_host`**(`127.0.0.1`)·**`http_port`**(`8000`)·**`auth_token`**(http 트랜스포트 필수). 돈 필드(`max_order_amount`·`daily_order_limit`·`max_order_amount_usd`·`daily_order_limit_usd`)는 `_no_float` validator 로 float 거부. `paper_starting_cash` 는 `_parse_starting_cash` validator 로 JSON dict·scalar·float·bool 처리(float/bool 거부). 사용자용 표는 `pytossinvest-mcp/README.md`.
