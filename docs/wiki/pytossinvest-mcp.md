> **언제 읽나**: `pytossinvest-mcp`(MCP 서버) 코드를 만질 때 — 툴 추가/수정, 안전모델(모드·가드레일·preview/confirm·멱등성) 손보기, paper 엔진·감사로그 작업. 안전 불변식과 모듈별 책임의 living 레퍼런스. (외부 API 는 [tossinvest-open-api.md](tossinvest-open-api.md), SDK 는 [pytossinvest-sdk.md](pytossinvest-sdk.md), 설계 시점 기록은 `docs/superpowers/`.)
>
> **🔄 자가갱신**: MCP 코드를 바꾸면(새 툴·모드·가드레일·config·라우팅·함정) **같은 세션에 이 문서를 갱신**한다. 커밋은 수동. 코드가 진실 — 어긋나면 발견 즉시 고친다.

# pytossinvest-mcp (MCP 서버) 내부구조

LLM(Claude Desktop/Cursor 등)에 토스 계좌 읽기/거래를 **안전하게** 쥐여주는 MCP 서버. **Apache-2.0**. `pytossinvest` SDK 의존. **stdio**(기본) 또는 **http** 트랜스포트.

- 위치: `pytossinvest-mcp/src/pytossinvest_mcp/`
- 테스트: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests` (FakeClient + paper 엔진, 256개, **라이브 키 불필요**)
- 의존: `mcp>=2.0.0,<3`(SDK v2 `MCPServer`, lock 2.2.0 — 2.1 부터 도구 예외 은닉, 아래 함정), `pydantic-settings`(직접 의존 — v2 부터 `mcp` 가 안 끌고 옴), `pytossinvest`. 옵션 extra: `redis = ["redis>=5"]`(HA 백엔드), `http = ["uvicorn>=0.30"]`(http 트랜스포트). dev extra: `fakeredis[lua]>=2`(테스트).

## 🔒 안전 불변식 (이 프로젝트의 핵심 — 절대 깨지 말 것)

> **체결 경로(`paper.place` / `client.place_order`)는 반드시 `safety.check_guardrails` 를 거친다.** confirmation 토큰은 `preview_order` 에서만, 가드레일 통과 후 발급된다. `place_order` 는 `executing(token)`(토큰당 실행은 한 번에 하나) 안에서 `consume(token)` → `check_guardrails(check_daily=False)` → **`reserve`(원자적·`clientOrderId` 멱등)** → 실행 → **성공 시 `commit`(일일 누적 확정) / 실패 시 `release`(예약 해제 — 체결이 확실히 안 된 실패만: paper 전부·첫 시도의 토스 4xx(`409 request-in-progress` 제외). 타임아웃·연결끊김·5xx 면 예약 유지, 그렇게 유지된 예약은 재시도가 거부돼도 풀지 않는다)** → 실패 시 토큰 유지, 동일 `clientOrderId` 로 멱등 재시도. **modify 도 동형**: `preview_modify`→`modify_order(confirmation_token)` — consume → 가드레일 재검사(**델타 회계** — `check_daily=True, prev_notional=원본명목`) → **`reserve(delta)`** → 실행 → 성공 시 **`commit`**(토큰 pop — 델타는 `reserve` 때 가산됨) / 실패 시 **`release(delta)`**(place 와 같은 확실한-실패 규칙, 토큰 유지). 델타는 증가분만(`max(0, new−old)`)이라 줄이는 정정은 한도를 환급하지 않는다. 다른 `issue_token` 호출자나 가드레일 우회 체결 경로를 만들지 말 것.

## 레이어 (의존 방향: 위 → 아래)

```
server.py     ← MCPServer build_server(version 명시) + transport_kwargs(전송 옵션 seam), run_server(stdio|http 분기), main()
http.py       ← ASGI 조립 + bearer 미들웨어 (BearerAuthMiddleware / build_http_app / serve_http)
tools.py      ← AppContext + 툴 함수 fn(app, ...). 라우팅(paper vs real). 테스트는 이 함수 직접 호출
  ├ market_data.py  시세 외 시장 데이터(수급 5종→1툴, 랭킹, 지표 현재가, 지표 캔들/지수 투자자별→1툴). 계좌 무관이라 모드 무관 real
  ├ conditional.py  조건주문: 요청 검증·명목 계산(pure) + 조건주문 툴 함수·실행 단계(place/modify 가 kind 로 호출)
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

- **`build_server`**: `MCPServer("pytossinvest-mcp", version=__version__, cache_hints=...)` 초기화 — **v2 는 전송 옵션(`stateless_http`·`transport_security`)을 생성자가 아니라 `streamable_http_app()` 인자로 받으므로** 서버 객체에는 안 실린다. `version=` 미지정 시 빈 문자열이 나가므로 명시(v1 은 그 자리에 SDK 버전이 새어 들어갔었다). 전송 옵션은 **`transport_kwargs(settings)`** 단일 seam — http 면 `{"stateless_http": True, "transport_security": ...}`, `http.py`·bench·테스트가 같은 걸 쓴다(안 넘기면 조용히 SDK 기본값 = 세션 모드 + localhost-only 보호로 떨어지는 게 v2 의 함정).
- **`run_server(settings, mcp)`**: http 면 `build_http_app(mcp, auth_token=settings.auth_token)` → `serve_http(app, host, port)`. stdio 면 `mcp.run()`.
- **`http.py`**: `BearerAuthMiddleware(BaseHTTPMiddleware)` — `hmac.compare_digest` 상수시간 bearer 검증(**bytes 비교** — str 은 비ASCII 에서 `TypeError`→500), 실패 시 401. `build_http_app(mcp, *, auth_token)` — `mcp.streamable_http_app()` 에 미들웨어 추가 후 Starlette 앱 반환. `serve_http(app, *, host, port)` — uvicorn runner(함수 내부에서만 `import uvicorn` — `[http]` extra).
- **직교성**: transport 축은 `MODE`(read_only/paper/live) 및 `STATE_BACKEND`(memory/redis)와 독립. 어떤 조합이든 동작. 다중 인스턴스 HA = `http` + `redis` 백엔드 조합 권장.
- **인스턴스 간 공유 범위** — redis 백엔드가 공유하는 건 spend 카운터·paper 상태·confirmation 토큰·**토스 액세스 토큰**(아래 '액세스 토큰 공유')이다. SDK 의 레이트리밋 버킷(`ratelimit.TokenBucket`)은 프로세스 로컬이라 인스턴스마다 따로 센다. 토스의 한도는 계정 단위라 합산하면 넘을 수 있고, 그때는 `X-RateLimit-*` 헤더 동기화와 429 유계 재시도로 수습한다. 공유 레이트리미터는 만들지 않았다(단일테넌트·저부하. 설계 시점 기록: `docs/superpowers/specs/2026-06-18-self-host-remote-mcp-design.md §8`).

## 가드레일 (`safety.check_guardrails`, 순서 중요)

`build_spec` 이 먼저 비양수 값(`quantity`·`price`·`order_amount` ≤ 0)을 `invalid-order-value` 로 거부. 그리고 `order_amount` 를 `price` 또는 `quantity` 와 동시 전달하면 `invalid-order-params` 로 거부(금액주문과 수량/가격 주문의 혼합 차단). 그다음 `check_guardrails` 가 아래 순서로 검사:

deny심볼 → allow심볼 → **하드실링 초과 무조건 거부**(`max-order-exceeded`) → **고액 + 미확인**(`confirm-high-value-required`) → 주문당 상한(`order-amount-cap`) → 일일 누적 상한(`daily-limit`, `check_daily=True` 일 때만) → 장시간(`enforce_hours` 일 때만, `market-closed`). **순서가 테스트를 통과시키는 핵심** — 재배열 금지.

**통화별 임계 및 상한** (시세 응답 통화 우선, 없으면 심볼 모양 — `order_currency(symbol)`):
| | KRW (숫자로 시작, 예 `005930`·`0101N0`) | USD (그 외, 예 `AAPL`·`BRK.B`) |
|---|---|---|
| 고액 confirm 임계 (`>=`) | `HIGH_VALUE_THRESHOLD` = 1억 | `HIGH_VALUE_THRESHOLD_USD` = $100,000 |
| 하드실링 (`>`) | `MAX_ORDER_THRESHOLD` = 30억 | `MAX_ORDER_THRESHOLD_USD` = $3,000,000 |
| 주문당 상한 | `config.max_order_amount` | `config.max_order_amount_usd` |
| 일일 누적 상한 | `config.daily_order_limit` | `config.daily_order_limit_usd` |

- **FX 환산 없음** — notional 은 주문통화 기준 비교. KRW/USD 버킷 분리(서로 막지 않음).
- **deny/allow 심볼 매칭은 정규화** — `check_guardrails` 에서 `spec.symbol` 과 리스트 양쪽을 `.strip().upper()` 로 정규화해 비교(대소문자·앞뒤 공백 무시). `spec.symbol` 자체는 변경 안 함 — 브로커에는 원본값이 그대로 전달된다.
- **modify 델타 회계** — modify(`preview_modify`→`modify_order`)는 `check_daily=True, prev_notional=원본명목`으로 호출 — 일일 버킷은 증가분(`spend_delta` = `max(0, new−old)`)만 검사·가산. per-order·고액·하드실링은 전액으로 검사. `reserve(delta)` → 성공 시 `commit` / 실패 시 `release`. 줄이는 정정은 취소와 마찬가지로 환급이 없다. 환급하면 MCP 가 센 적 없는 주문(앱에서 낸 주문)을 줄여 누계를 낮추고 그만큼 새 주문을 더 낼 수 있기 때문이다.
- 장시간 게이트는 **live 전용** — `tools._market_gate(app, symbol, currency)` 가 `enforce = config.enforce_market_hours and app.is_live`. paper 는 아무때나 데모 가능. **국가 판정은 권위 통화 우선** — `_country_for_order(symbol, currency)`: `currency` 가 `USD`→`US`·`KRW`→`KR`(정규화 후), 없으면 `order_currency(symbol)` 폴백(가드레일과 같은 함수). `preview_order`·`preview_modify` 가 `spec.currency`(C1 권위 통화)를 넘겨 가드레일 통화와 장시간 게이트 국가가 한 소스로 일치.

## preview → place / preview_modify → modify 토큰 생애 (`safety.py`)

- `build_spec(...)` — 비양수 검증(`invalid-order-value`) → `order_amount` + `price`/`quantity` 동시 전달 거부(`invalid-order-params`) → notional 계산(precedence: `order_amount` → `price*quantity` → `ref_price*quantity` → `GuardrailError("insufficient-order-params")`) + `clientOrderId` 자동 부여(`gen_id`) + `currency` 파라미터 우선, 없으면 `order_currency(symbol)` 폴백 + `modify_order_id` 셋. **`prev_notional` 은 `build_spec` 인자가 아니다** — `preview_modify` 가 `build_spec` 호출 후 `spec.prev_notional`(원본 price×qty)에 직접 대입한다. `tools.py` 의 `preview_order`·`preview_modify` 는 `_price_and_currency(app, symbol)` 로 `get_prices` 한 번 → 권위 통화를 주입; 조회 실패/공백 시 폴백.
- `issue_token(spec)` — token_store 에 `(spec, expires_at=now+ttl, issued_at=now)` 저장(TTL 포함). `now` 는 **벽시계 epoch 초**(`server.py` 가 `time.time` 주입) — redis 에 저장되므로 monotonic 이면 재시작·다중 호스트에서 틀어진다. **`preview_order`·`preview_modify` 가 check_guardrails 통과 후에만 호출**.
- `consume(token)` — 존재·만료 검증 후 spec 반환. **pop 안 함**(만료면 삭제 후 `expired-confirmation`, 없으면 `invalid-confirmation`). **live 최소지연 게이트**: `config.live_confirm_min_delay_sec > 0` 이고 `config.is_live` 이면 `now - issued_at < delay` 일 때 `confirm-too-soon`.
- `executing(token)` — place/modify 한 번의 실행 동안 토큰을 잡는 컨텍스트매니저(`token_store.claim` / `unclaim`). 같은 토큰의 호출이 겹치면 두 번째는 `confirmation-in-progress` 로 거부한다. 같은 토큰은 예약 하나를 공유하므로, 두 번째 호출이 브로커까지 가서 거부당하면 첫 번째(성공하는) 호출이 기대는 예약을 풀어 버린다. `unclaim` 중의 store I/O 오류는 삼킨다(claim 은 TTL 로 사라진다. 여기서 예외를 올리면 방금 체결된 주문이 실패로 보여 새 preview 로 다시 나간다).
- `send_within(token)` — 이 토큰의 주문을 아직 보내도 되는 남은 초. 같은 토큰의 시도는 모두 `clientOrderId` 하나를 쓰는데, 토스는 그 id 를 처음 본 뒤 10분(`IDEMPOTENCY_WINDOW_SEC`)만 중복으로 걸러 준다. 재시도가 SDK 안의 대기(레이트 게이트·429 백오프)로 그보다 늦게 전송되면 새 주문이 된다(주문 2건, 예약 1건). 그래서 `preview 시각 + 600 − SEND_MARGIN_SEC(30)` 을 시한으로 잡아 SDK 의 `send_within` 으로 넘기고, SDK 는 시한이 지나면 보내지 않는다. preview 는 첫 전송보다 늦을 수 없으니, 거기서부터 세면 모든 전송이 창 안에 든다. 여유 30초는 요청이 가는 시간과 인스턴스 간 시계 차이 몫이다. 시한에 걸리면 `place_order` 는 `send-deadline-exceeded` 로 끝나고, 메시지로 새 preview 전에 주문 목록부터 확인하라고 알린다(앞선 시도가 체결됐을 수 있다). 첫 시도였다면 예약을 풀고, 재시도였다면 유지한다. 정정에는 멱등 키가 없어 해당 없다.
- `attempt(spec)` — `reserve` 전에 읽는다. 이 `clientOrderId` 의 예약이 이미 있으면 `Attempt.RETRY`(앞선 호출이 체결 불명으로 끝나 예약이 남아 있다), 없으면 `Attempt.FIRST`.
- **reserve-first place 흐름**: `place_order` 는 `executing(token)` 으로 토큰을 잡고 `consume` 후 `check_guardrails(check_daily=False)` → `tools._reserve`(`store.reserve(day, currency, notional, cap, clientOrderId)` 뒤 감사로그에 `decision: reserved` 를 먼저 쓴다. 이 줄이 재시작 복원의 근거라, 못 쓰면 예약을 풀고 `state-unavailable` 로 끝내며 주문은 보내지 않는다) → 실행 → 성공 시 `tools._settle_success`(`commit(token)` + 감사 `placed`) / 실패 시 `tools._settle_failure` 가 `_is_rejection` 이면 `store.release(day, currency, notional, clientOrderId)`, 아니면 예약 유지(감사 `reservation: released|kept`). `reserve` 는 원자적(`clientOrderId` 멱등 — 같은 키로 중복 예약 시 기존 결과 반환). 실패해도 토큰은 살아있어 재시도 가능.
- **reserve-first modify 흐름**: `modify_order` 는 `consume` 후 `check_guardrails(check_daily=True, prev_notional=spec.prev_notional)` → `store.reserve(day, currency, delta, cap, clientOrderId)` → 실행 → 성공 시 `store.commit(token)` / 실패 시 place 와 같은 `_settle_failure`. delta = `max(0, new−old)`. 체결 불명으로 유지된 modify 를 재시도했는데 첫 시도가 이미 반영돼 토스가 4xx 를 주는 경우(`Attempt.RETRY`)에도 예약은 남는다.
- `restore_spend(events)` — **memory 백엔드 전용**. 부팅 시 audit `read_events()` 결과에서 당일(`ts` UTC → KST 날짜 변환) 이벤트를 읽어, 아직 서 있는 예약을 SpendStore 에 `seed` 한다. 센다: 보내기 전에 쓴 `reserved`, 그리고 `placed`·`modified`. `clientOrderId` 하나당 한 번만 세고, 그 id 에 `error` + `reservation: released` 가 있으면 뺀다. 그래서 결과를 못 남긴 호출(체결 불명으로 `kept`, 또는 호출 도중 프로세스 종료)도 재시작 뒤 한도에 남는다. `clientOrderId` 가 없거나 문자열이 아닌 줄(옛 로그, 손상된 줄)은 줄마다 따로 센다. dict 가 아닌 이벤트, `notional`/`ts` 누락, 파싱 불가 값은 건너뛴다(손상 감사 파일이 있어도 부팅된다). 감사 파일이 없거나 지워지면 누적이 0 으로 돌아간다. **redis 백엔드에서는 `seed` 가 no-op** — counter 가 AOF 로 지속되므로 다시 넣으면 이중 계산이다.
- **성공 뒤의 기록 실패** — 브로커 호출이 성공한 뒤 `commit` 이나 감사 기록이 실패하면 `_settle_success` 가 예외 대신 결과에 `warning` 을 붙여 돌려준다. 증상(바꾸기 전): redis 가 잠깐 끊기면 이미 나간 주문이 툴 오류로 보였고, 모델의 자연스러운 대응은 새 preview 로 같은 주문을 한 번 더 내는 것이었다. 예약은 이미 세고 로그에 남겼으므로 한도는 잃지 않는다. 실패 쪽(`_settle_failure`)의 예약 해제와 감사 기록도 best-effort 다: 스토어 장애가 브로커의 실제 오류를 가리면 안 된다. 해제하지 못한 예약은 그대로 남는다(과다계상).
- 멱등성: place/modify 실패 시 토큰 유지 → 동일 `clientOrderId` 로 멱등 재시도. **예약은 `_is_rejection` 일 때만 해제** — `Attempt.RETRY` 면 모드와 무관하게 해제하지 않는다. 첫 시도일 때 paper 는 로컬 체결이라 실패하면 해제하되 paper 스토어 I/O 오류(`_STORE_ERRORS`)는 예외다(redis 에는 체결이 저장된 뒤 끊겼을 수 있다). live 는 `TossInvestError` 이고 `http_status < 500` 이며 코드가 `_UNSETTLED_CODES`(`request-in-progress`: 같은 `clientOrderId` 가 아직 처리 중, `duplicate-conditional-order`)가 아닐 때만. RETRY 에서 해제하지 않는 이유: 401·429 는 주문 처리에 닿지 못한 응답이라 앞선 시도에 대해 아무것도 말해 주지 않고, 키가 없는 정정은 재전송이 거부돼도 첫 정정이 반영됐을 수 있다. 타임아웃·연결끊김·5xx 는 체결됐을 수 있어 유지: 같은 토큰 재시도는 dedup 키로 재사용(두 번 안 셈), 새 preview 는 그 위에 쌓여 이중 주문이 한도를 못 넘는다. 확실히 안 된 주문의 예약이 남으면 그날 한도가 그만큼 보수적으로 줄어든다(memory 백엔드는 재시작 시 사라짐 — 복원은 `placed`/`modified` 만 센다). `commit` 후에는 토큰 pop 되어 2차 발사 불가.
- **place 시 일일한도 재검사**: `place_order` 는 `consume` 직후 실행 전에 `check_guardrails(spec, ..., check_daily=False)` → `reserve` 가 원자적으로 cap 검사 — preview 를 여러 개 발급해 한도를 초과하는 우회 차단.

## 상태 백엔드 (memory | redis)

`safety.py` 는 `TokenStore` / `SpendStore` 두 인터페이스를 통해 상태를 읽고 쓴다. 백엔드는 `TOSSINVEST_STATE_BACKEND`(`memory`기본 / `redis`) 환경변수로 선택된다.

### TokenStore 인터페이스

| 메서드 | 설명 |
|---|---|
| `put(token, spec, *, expires_at, issued_at)` | 확인 토큰 저장(`SafetyManager.issue_token`) |
| `get(token)` | `(spec, expires_at, issued_at)` 또는 `None`. 지우지 않는다(`consume`) |
| `delete(token)` | 성공 확정(`commit`) 또는 만료 시 삭제 |
| `claim(token) -> bool` | 실행권 획득. 다른 호출이 실행 중이면 `False`(`executing`) |
| `unclaim(token)` | 실행권 반납 |

### SpendStore 인터페이스

| 메서드 | 설명 |
|---|---|
| `reserve(day, currency, delta, cap, dedup_key) -> bool` | 원자적 예약 — 한도 초과 시 False, `dedup_key`(`clientOrderId`) 기반 멱등 |
| `release(day, currency, delta, dedup_key)` | 예약 해제(해당 `dedup_key` 예약이 있을 때만) |
| `reserved(day, dedup_key) -> bool` | 그 `dedup_key` 의 예약이 남아 있는지(`attempt`) |
| `current(day, currency) -> Decimal` | 당일 누적 |
| `seed(day, currency, amount)` | 부팅 복원용 — memory: 직접 주입; **redis: no-op** |

### memory 백엔드 (`stores.py`)

기본값. 단일 인스턴스, 재시작 시 휘발. 부팅 시 `restore_spend(audit_events)` 로 당일 누적 복원(`placed`+`modified` 이벤트 합산). mcp SDK 가 sync 툴을 워커 스레드에서 실행하므로 병렬 툴콜이 스토어에 동시에 들어온다. 그래서 `MemorySpendStore` 의 `reserve`/`release`/`reserved`/`seed` 와 `MemoryTokenStore` 의 `claim`/`unclaim` 은 `threading.Lock` 아래에서 돈다(확인 후 갱신이 한 덩어리여야 `reserve` 가 원자적이다).

### redis 백엔드 (`redis_stores.py`)

`redis>=5` + `fakeredis[lua]>=2`(dev/test). `TOSSINVEST_REDIS_URL` 필요.

- **SpendStore**: day-scope key(`spend:{day}:{currency}`)에 Decimal 문자열 저장. `reserve`/`release` 는 redis-py `transaction()`(`WATCH`/`MULTI`)이다: 키를 감시한 채 읽고 Python Decimal 로 계산한 뒤 쓰며, 그 사이 키가 바뀌었으면 통째로 다시 한다. TTL 락을 쓰지 않는 이유: 락을 쥔 인스턴스가 TTL 보다 오래 멈추면 락이 풀린 사이 다른 인스턴스가 한 예약을 뒤늦은 쓰기가 덮어쓴다. **`INCR`/`INCRBYFLOAT` 금지** — 돈은 float 불가, Decimal 직렬화만.
- **dedup set**: `reserved:{day}` Set에 `clientOrderId` 저장으로 중복 예약 방지(멱등성). day-scoped 만으로 충분 — `clientOrderId` 가 전역적으로 고유하므로 통화를 키에 포함할 필요 없음.
- **실행권(claim)**: `claim:{token}` 키를 `SET NX EX`(`CLAIM_TTL_SEC`)로 잡는다. 여러 인스턴스가 한 redis 를 공유해도 토큰당 실행은 하나다. TTL 은 한 번의 시도가 걸릴 수 있는 시간(SDK 타임아웃 + 유계 429 재시도)보다 길게 잡았고, 인스턴스가 실행 도중 죽었을 때만 의미가 있다.
- **`seed` no-op**: redis counter 가 진실의 원천(AOF 지속). 감사 파일을 지워도 redis 카운터는 유지.
- **Lock.release()**(paper 락과 액세스 토큰 락이 쓴다): redis-py 내부적으로 EVALSHA(Lua) 사용 → 테스트에서 `fakeredis[lua]` 서브패키지 필수(없으면 `ResponseError`). 실제 Redis 는 Lua 기본 내장.

### RedisAuditSink (`audit.py`)

감사 이벤트를 Redis Stream(`audit`)에 기록. JSONL 파일 감사와 병렬 사용 가능. `read_events()` 는 Stream에서 전체 이벤트 조회.

### fail-closed 래퍼 (`_guard_store`)

store I/O (`reserve`/`release`/`commit`/`seed`/`claim`) 중 `safety._STORE_ERRORS` 가 나면 → `GuardrailError("state-unavailable")`로 변환(fail-closed). `_STORE_ERRORS` 는 내장 `ConnectionError`/`TimeoutError`/`OSError` 에 redis-py 의 `RedisError` 를 더한 것이다. redis-py 의 `ConnectionError`·`TimeoutError` 는 내장 예외를 상속하지 않아서, 내장 예외만 잡으면 redis 백엔드에서는 변환이 일어나지 않는다. `GuardrailError` 자체는 절대 삼키지 않는다 — 항상 호출자에게 전파.

### 액세스 토큰 공유 (`token_cache.py`)

토스는 client id 당 유효한 액세스 토큰을 하나만 둔다. 새로 발급하면 이전 토큰이 죽으므로, 한 키를 쓰는 인스턴스가 각자 발급하면 서로의 토큰을 계속 무효로 만든다. redis 백엔드에서는 `server.build_client` 가 SDK 클라이언트에 `RedisTokenCache` 를 `token_cache` 로 넘겨, 한 인스턴스만 발급하고 나머지는 그 토큰을 가져다 쓴다(SDK 쪽 동작은 [pytossinvest-sdk.md](pytossinvest-sdk.md) 의 '프로세스 간 토큰 공유').

- **저장**: 키 `oauth:token:{client id 해시}` 에 Fernet 으로 암호화해 넣고, 만료는 redis TTL 로 건다. 암호화 키는 client secret 에서 HKDF 로 유도한다. redis 에는 secret 이 없으므로 redis 를 읽을 수 있다는 것만으로는 토큰을 얻지 못한다. 평문으로 두면 redis 읽기 권한이 가드레일을 전부 건너뛰는 API 권한이 된다.
- **발급 직렬화**: `lock:oauth:token:{해시}` 락을 잡고 load, 없으면 발급 후 save.
- **폐기**: `discard(token)` 은 저장된 토큰이 그 토큰일 때만 지운다(`WATCH`). 다른 인스턴스가 이미 새 토큰으로 바꿨다면 건드리지 않는다.
- **장애 시**: redis 오류는 전부 삼키고 '공유 없음'으로 떨어진다(각자 발급. 캐시가 없을 때와 같다). 캐시는 안전장치가 아니라서 fail-open 이다. redis-py 기본 재시도 때문에 죽은 redis 에는 호출 하나가 몇 초씩 걸리지만, 캐시를 건드리는 건 토큰을 발급·폐기할 때뿐이다.
- **공유되지 않는 경우**: redis 가 따로인 배포끼리(예: 로컬 Docker 와 OCI)는 여전히 서로의 토큰을 죽인다. 코드로 풀 방법이 없다. SDK 가 재발급을 두 번까지 해서 버티지만, 한 키는 한 배포(한 redis)에서 쓰는 것이 맞다.

### PaperStore 심 (`paper.py` + `redis_stores.py`)

`PaperBroker`는 `PaperStore` 인터페이스(`lock() → context manager`, `load() → PaperState`, `save(state)`)를 통해 상태를 읽고 쓴다. 백엔드는 `state_backend` 설정값에 따라 `server.py` 의 `_build_stores`(4-tuple 반환)에서 선택된다.

**`MemoryPaperStore`** (기본, `paper.py`):
- 인스턴스 로컬 — 재시작 시 휘발. `lock()` 은 `threading.Lock`, `load()` 는 스냅샷(아래 '한계').

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

- **감사 파일의 찢어진 줄.** 디스크가 가득 차 한 줄이 중간까지만 쓰이면 줄바꿈이 없어 다음 줄이 거기에 붙고, 둘 다 파싱에서 버려진다. 그 다음 줄이 체결 불명으로 끝난 주문의 `reserved` 였다면 그날 재시작 뒤 누계에서 빠진다.
- **버전 혼용.** spend 카운터를 TTL 락으로 쓰던 버전과 `WATCH/MULTI` 버전을 한 redis 에 같이 띄우면 구버전의 읽기-쓰기 사이에 신버전의 예약이 덮어쓰일 수 있다. 여러 인스턴스를 올릴 때는 한 번에 바꾼다.


`MemoryPaperStore.lock()` 은 `threading.Lock` 이다(병렬 툴콜의 체결이 같은 현금을 두 번 쓰지 않게). `load()` 는 redis 스토어처럼 스냅샷을 준다: 조회는 락 없이 돌고, 도중에 실패한 체결은 저장된 상태를 건드리지 않는다. 포지션은 복사하고, 기록된 뒤 바뀌지 않는 주문은 공유한다. `RedisPaperStore` 는 분산락 `lock:paper`.

transport는 `stdio`(기본, 단일 클라이언트) 또는 `http`(원격 다중 클라이언트 가능). 다중 인스턴스 redis paper 공유는 가능하나 동시 paper 충돌 시 lock 대기 시간(기본 5s)이 생길 수 있다. paper 락은 TTL 락 그대로다(모의 잔고라 spend 카운터처럼 트랜잭션으로 바꾸지 않았다). 다중 인스턴스 HA = `http` + `redis` 백엔드 조합.

## 24 툴 (`server.py` 등록, `tools.py`·`conditional.py`·`market_data.py` 구현)

- **파라미터 enum**: `side`=`BUY|SELL`, `order_type`=`LIMIT|MARKET`, `time_in_force`=`DAY|CLS|OPG`, `list_orders.status`=`OPEN|CLOSED`(+`cursor` — CLOSED 는 페이지네이션, 응답 `nextCursor` 를 넘김), `currency`=`KRW|USD` (`server.py` `Literal` 별칭 → JSON 스키마 enum, 위반은 SDK 인자검증 `ToolError` 로 모델에 보임). `preview_order` 설명은 정수 `quantity` 우선, `order_amount`(US MARKET 금액·소수점)는 요청 시에만.
- **시장 데이터(항상, 모드 무관 real — `market_data.py`)**: `get_stock_trends(symbol, kind)`(수급 5종 엔드포인트를 `kind` 로 한 툴에), `get_rankings`(기본 `count=20` — API 기본 100 은 컨텍스트 과다), `get_market_indicators(symbols)`(KOSPI·KOSDAQ·KR_BOND_*), `get_indicator_history(symbol, view)`(`candles` 는 `before`, `investor_trading` 은 `until` 로 페이지 — 다른 view 커서는 `ValueError`). `/stocks/all` 은 시장 전체 덤프라 **MCP 미노출**(SDK 만).
- **읽기(항상)**: `get_accounts`·`get_holdings`(현금 없음 — API 스펙)·**`get_buying_power(currency=None)`**(통화 생략 시 KRW·USD 둘 다, `[{currency, cashBuyingPower}]`; paper 는 paper 현금 버킷, 그 외 `GET /buying-power`. `read_only` 에서도 현금을 볼 수 있는 유일한 경로 — `get_order_readiness` 는 쓰기 쪽이라 read_only 에 없음. 총 예수금은 API 에 없다)·`get_quote`(단일심볼이면 orderbook+trades 동봉)·`get_candles`·`get_stock_info`·`get_market_info`(calendar + 옵션 FX)·`list_orders`·`get_order`
- **쓰기(read_only 외)**: `get_order_readiness`·`preview_order`→`place_order`·**`preview_modify`**→`modify_order(confirmation_token)`·`cancel_order`
  - `preview_modify(order_id, order_type, price=None, quantity=None, confirm_high_value_order=False)` — live 전용. 원주문 조회 → 병합 → `build_spec(modify_order_id=order_id, prev_notional=원본price×qty, currency=권위통화)` → `check_guardrails(check_daily=True, prev_notional=…)` → `issue_token` → 감사(`modify_previewed`, previousStatus).
  - `modify_order(confirmation_token)` — live 전용. `consume` → `check_guardrails(check_daily=True, prev_notional=spec.prev_notional)` 재검사 → `reserve(delta)`(증가분만) → `client.modify_order` → 성공 시 `commit(token)` / 실패 시 `release(delta)`, 토큰 유지 → 감사(`modified`, notional=delta, currency).
  - `cancel_order(order_id)` — live 전용. 원주문 조회 → **`safety.check_symbol`(deny/allow)** → 취소, `previousStatus` 를 감사(`canceled`)에 기록.
- 출력 돈/수량은 전부 **문자열**(`_paper_order_dict`·holdings 등에서 `str()`). 툴 description 에 "string money / 2단계 주문 / live-only" 명시(LLM 가이드).

## 조건주문 (`conditional.py`)

감시가에 닿으면 브로커가 스스로 내는 주문(SINGLE / OCO / OTO, `expire_date` 까지). 설계 기록 `docs/superpowers/specs/2026-09-30-conditional-orders-mcp-design.md`.

| 모드 | `list_conditional_orders`·`get_conditional_order` | `preview_conditional_order`·`preview_conditional_modify`·`cancel_conditional_order` |
|---|---|---|
| `read_only` | 실계좌 | 미등록 |
| `paper` | 빈 목록 / `ValueError`(없음) | `PaperError`(live 전용 — paper 엔진은 즉시체결이라 감시 시뮬 없음) |
| `live` | 실계좌 | 실계좌 |

- **토큰 경로 공유**: preview 가 `build_conditional_spec` 으로 `OrderSpec(kind="conditional", conditional=<build_request 결과 = SDK kwargs>)` 를 만들어 토큰 발급 → **기존 `place_order`/`modify_order`** 가 consume → `check_guardrails(check_daily=False)` → `reserve` 까지 그대로 하고 실행 단계에서만 `conditional.execute_place/execute_modify` 호출 → `_settle_failure`/commit/감사도 공용. 감사는 `placed`/`modified` 에 `kind: "conditional"` 만 붙여 `restore_spend` 가 그대로 센다.
- **로컬 검증**(`build_request`): 숫자 유한 양수(`invalid-order-value`), `expire_date` ISO 날짜·SINGLE 은 second 금지·OCO/OTO 는 second 필수·LIMIT 은 다리마다 주문가 필수·MARKET 은 주문가 금지(`invalid-order-params`). "OCO 둘 다 SELL"·감시가↔현재가 관계·OCO/OTO LIMIT 전용·종목당 1개 같은 **토스 의미 규칙은 재구현 안 함**(토스 4xx 가 모델에 보임).
- **명목**: 다리 가격 = `orderPrice`(LIMIT) / `triggerPrice`(MARKET), 다리 금액 = 수량 × 가격. SINGLE = 한 다리, **OCO = 큰 다리**(하나만 발동), **OTO = 두 다리 합**(둘 다 발동). 통화는 시세 통화 → `order_currency` 폴백. 가드레일은 일반 주문과 동일, **장 시간 게이트만 생략**(등록은 아무 때나, 발동은 세션 중).
- **일일 한도(사용자 결정)**: 등록한 날(KST) **전액** 예약 — 발동 여부 무관. 수정은 **`max(0, new − old)`**(`SafetyManager.spend_delta` — 조건주문은 만기까지 살아서 전날 센 주문일 수 있으니 축소가 오늘 한도를 환급하면 "축소 후 취소"로 취소-무환급 규칙이 뚫린다. 일반 주문 정정도 같은 규칙을 쓴다). old = `get_conditional_order` 상세로 같은 규칙 계산, 가격 없는 다리(앱에서 만든 `PROFIT_RATE` 등)나 예상 밖 응답 형태면 old 미상 → 새 금액 전액. **취소는 환급 없음**. 확실한-거부 release 규칙 그대로, 단 `duplicate-conditional-order` 4xx 는 **예약 유지**(`_UNSETTLED_CODES` — 타임아웃 뒤 실제 등록된 주문을 같은 토큰으로 재시도하면 토스가 멱등 응답 대신 종목당 1개 규칙으로 거절할 수 있다).
- **토큰 용도·격리**: `place_order` 는 정정 토큰, `modify_order` 는 주문 토큰을 `wrong-token` 으로 거부(정정 토큰은 차액만 예약 — 일반 주문에도 적용). 실행 단계(`execute_place/execute_modify`)는 `_require_live` — paper 인스턴스가 공유 redis 의 조건주문 토큰을 받아도 실브로커로 안 나간다. redis 에선 조건주문 토큰을 **`ctok:`** 키에 둔다(`RedisTokenStore` — 조건주문 이전 빌드가 같은 redis 를 보면 `tok:` 만 읽어 조건주문 spec 을 일반(시장가일 수 있는) 주문으로 즉시 실행할 수 있어서).
- **취소**: 확인 토큰 없이 한 번 호출이지만 **deny/allow 목록을 적용**(`check_symbol`) — 앱에서 건 손절일 수 있어 "취소는 위험을 줄인다"가 성립하지 않는다(2026-09-30 사용자 결정: 2단계 토큰 대신 목록만). 한도 환급 없음.
- **멱등성**: 등록은 `clientOrderId` 전달 → 같은 토큰 재시도 안전. 수정은 키가 없어, 반영된 수정을 재시도하면 옛 id 가 4xx 를 받는다. 이때는 `Attempt.RETRY` 라 예약을 풀지 않는다(주문 정정과 같은 규칙).
- **라이브 검증 범위**: OCI 는 `read_only` 라 조회만 실측. 쓰기는 실제 조건주문이 걸리므로 라이브로 돌리지 않았다(FakeClient 테스트 + `call_tool` 왕복만).

## 모듈별 함정 (이미 겪은 것)

- **툴은 워커 스레드에서 병렬로 돈다** — mcp SDK 가 sync 툴을 `anyio.to_thread` 로 실행한다. `AppContext` 하나(SDK 클라이언트·`SafetyManager`·스토어·paper)를 여러 스레드가 동시에 쓰니, 가변 상태에는 락이 필요하다. 증상은 병렬 `place_order` 가 같은 한도 잔액을 읽고 둘 다 통과하는 것(memory 스토어), 같은 토큰의 병렬 호출 중 하나가 거부되며 다른 쪽이 성공시킨 주문의 예약까지 해제하는 것. 대처는 스토어 락과 `executing(token)`. 새 상태를 추가하면 `tests/test_mcp_concurrency.py` 방식으로 겹침을 강제해 테스트한다.
- **paper modify/cancel 은 live 전용** — paper 즉시체결이라 미체결 주문 없음 → `PaperError`(실제 `409 already-filled` 미러링).
- **paper MARKET 무가격 체결 금지** — 체결 시점 `_ref_price` 가 None 이면 가격 0 으로 조용히 체결되던 버그 → `PaperError`(토큰 살림, 재시도 가능). US 금액주문 qty=amount/price 는 Decimal 나눗셈.
- **market_hours US 자정넘김** — 미국장 KST 표기는 23:30→06:00 처럼 wrap. `start>end` 면 `now>=start or now<end`. 깨진 시간 문자열은 "닫힘"(safe).
- **테스트 import** — `from conftest import FakeClient` (pytest 가 `tests/` 를 sys.path 에). `from tests.conftest` 는 `tests` 패키지 없어 깨짐.
- **call_tool 반환 형식 의존 금지** — MCP 버전마다 다름. 서버 테스트는 `list_tools()`(이름)로 검증, 동작은 `tools.py` 함수 직접 호출로 검증. 예외: `test_domain_errors_reach_the_model` 은 `call_tool` 이 `ToolError`(≠`UnexpectedToolError`)를 던지는지로 **SDK 의 예외 가시성 계약**을 고정한다.
- **도구 예외 은닉(mcp ≥2.1)** — SDK 가 `ToolError`/`MCPError` 가 아닌 예외를 crash 로 보고 모델엔 `Error executing tool <name>` 만 보낸다. 2.0.0 → 2.2.0 을 그냥 올리면 `daily-limit` 같은 거부 사유가 사라지는 걸 테스트로 확인했다. `server._model_facing_tool(mcp)` 가 돌려주는 `mcp_tool` 데코레이터가 `_MODEL_FACING_ERRORS`(`GuardrailError`·`PaperError`·`TossInvestError`·`ValueError`·`httpx.HTTPError`)를 `ToolError(str(e))` 로 바꿔 올린다(`functools.wraps` 라 입력 스키마 불변). 그 밖의 예외는 SDK 기본대로 숨김.
- **통화 판정 — 권위 통화 + 폴백(C1)** — `preview_order`/`preview_modify` 는 `_price_and_currency(app, symbol)` 로 `get_prices([symbol])` 한 번을 호출해 `Price.currency`(권위 통화)를 얻고 `build_spec(currency=…)` 로 주입. 조회 실패·빈 결과·공백 통화면 `order_currency(symbol)` 폴백(숫자로 시작→KRW, 그 외→USD — 틀려도 한도가 작은 USD 쪽). 예전 `isalpha()` 폴백은 `BRK.B`·`BF-B` 를 KRW 로 봐 USD 주문에 KRW 숫자 한도(주문당 1,000,000)를 적용하던 구멍이었다(2026-09-30 수정). `order_currency` 자체는 폴백 경로로만 남음. FX 환산 없음. KRW/USD 버킷 분리 유지. **이 권위 통화는 장시간 게이트 국가 판정에도 재사용**(`_market_gate`→`_country_for_order`) — 가드레일 통화와 미장/한국장 판정이 동일 소스. 통화가 없을 때만 `_country_for_order` 가 같은 `order_currency` 로 폴백.
- **M1 modify 델타 회계** — `preview_modify`·`modify_order` 는 `check_daily=True, prev_notional=원본명목` 으로 호출하고, 일일 버킷에는 증가분(`max(0, new−old)`)만 검사·가산한다. per-order·고액·하드실링은 전액 검사. 줄이는 정정을 credit 으로 반영하던 때의 증상: MCP 로 90만을 주문하고 앱에서 낸 70만 주문을 MCP 로 10만으로 줄이면 누계가 30만이 되어 60만을 더 낼 수 있었다(한도 100만에 150만). 원인: 0-하한은 누계가 음수가 되는 것만 막고, 센 적 없는 주문의 감액이 이미 센 금액을 지우는 것은 막지 못한다. 대처: `spend_delta` 가 음수를 내지 않는다. 스토어의 0-하한 코드는 남아 있다.
- **부팅 복원(UTC ts → KST 날짜)** — `restore_spend` 는 감사 이벤트의 `ts`(UTC ISO) 를 `datetime.fromisoformat(ts).astimezone(_KST).date()` 로 변환해 오늘 KST 날짜와 비교. `placed` 와 `modified` 이벤트의 `notional`·`currency` 를 합산한 뒤 통화별 0-하한 적용. 파싱 실패·dict 가 아닌 이벤트·`notional`/`ts` 필드 누락은 건너뜀(손상 감사 파일 있어도 서버 부팅 불가 없음). 감사 파일을 지우면 당일 누적도 0으로 리셋된다(주의).
- **`invalid-order-value` (양수 검증)** — `build_spec` 에서 `quantity`·`price`·`order_amount` 가 전달된 경우 `<= 0` 이면 `GuardrailError("invalid-order-value")`. notional 이 음수여서 상한 게이트를 조용히 통과하던 구멍 차단. **파싱 불가·비유한 값도 여기서 거부**(`_is_positive_number`: `"ten"`·`""`·`NaN`·`Infinity`) — 전엔 `decimal.InvalidOperation` 이 새어 나가 mcp ≥2.1 에서 모델엔 이유 없는 `Error executing tool` 만 보였다. float 은 계속 `to_decimal` 의 `TypeError`(호출자 버그라 그대로 둠).
- **http 트랜스포트 인증** — `TOSSINVEST_TRANSPORT=http` 로 부팅 시 `TOSSINVEST_AUTH_TOKEN` 이 비어있으면 `_http_requires_auth_token` model_validator 가 `ValueError` 발생(live/redis 와 동형 삼중 fail-closed). bearer 는 `hmac.compare_digest` 상수시간 비교 — 타이밍 공격 방지. 토큰은 엔드포인트 인증 전용, 단일 테넌트 — Redis 미저장, Toss API 자격증명 아님. `serve_http` 는 `uvicorn` 을 함수 내부에서만 import → stdio 설치 및 테스트 스위트는 uvicorn 없이 동작.
- **http 모드 DNS 리바인딩 보호 비활성화** — `transport_kwargs(settings)` 가 `transport=="http"` 일 때 `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)` 를 만들어 **`streamable_http_app()` 호출부**(`build_http_app` 경유)에 전달한다(v2 는 이 인자를 서버 생성자가 아니라 앱 빌더가 받는다 — 안 넘기면 조용히 SDK 기본값으로 폴백하는 게 함정). SDK 기본값은 host가 localhost 계열이면 DNS 리바인딩 보호를 자동 활성화해 비-localhost `Host` 헤더를 **421**으로 거부하는데, 원격 배포(Docker/리버스프록시)에서는 `mcp.example.com` 등 외부 호스트 헤더를 쓰므로 모든 요청이 421 처리된다. 인증 표면은 bearer 미들웨어(`BearerAuthMiddleware`)이고 배포 host 는 운영자/프록시가 제어하므로 Host 헤더 기반 방어가 불필요 — 의도적으로 비활성화. **stdio 경로는 이 인자를 전달하지 않아 기존 그대로.** 단, 운영자가 자기 host 를 알면 `TOSSINVEST_HTTP_ALLOWED_HOSTS`(JSON 리스트, 기본 `[]`)로 **opt-in Host 핀닝**을 켤 수 있다 — 비면 위처럼 보호 off, 비어있지 않으면 `enable_dns_rebinding_protection=True` + `allowed_hosts=<목록>` 으로 전달해 목록에 없는 `Host`→421(심화방어). 매칭은 정확일치 + `host:*` 포트 와일드카드(mcp `TransportSecurityMiddleware`). 빈 문자열 env 값은 JSON 파싱 실패로 부팅 거부되므로 deploy 템플릿은 해당 env 를 주석 처리(설정 시에만 비-빈 JSON 리스트).

## 새 툴 추가 절차

1. `tools.py` 에 `fn(app, ...) -> dict` 추가. 계좌컨텍스트면 `if app.use_paper:` 분기(paper 엔진 vs `app.client`). 돈은 문자열 출력.
2. write 툴이면 안전 불변식 준수 — preview→place 패턴(또는 preview_modify→modify 패턴)이면 `check_guardrails`/토큰 거치고, 감사로그 기록. `check_daily` 플래그를 올바르게(place=True, modify=True+prev_notional=원본명목).
3. `server.py` 의 `_register_reads`/`_register_writes` 에 **`@mcp_tool(name, description)`**(≠ `@mcp.tool` — 도메인 예외를 `ToolError` 로 올림) 클로저 추가 — `app` 캡처, `tools.fn(app, ...)` 위임. description 에 제약(문자열 머니 / live-only 등) 명시.
4. 테스트: `tools.py` 함수 직접 호출(FakeClient) + 모드별 등록은 `test_server_modes`.
5. 이 문서 갱신(가드레일 순서·툴 수·함정).

## config (env `TOSSINVEST_`)

`mode`·`allow_live`·`client_id`·`client_secret`·`base_url`·`account_seq`(기본 없음 = 첫 계좌. `server.build_client` 가 SDK 클라이언트의 `account_seq` 로 넘긴다)·`max_order_amount`(1,000,000 KRW)·`daily_order_limit`(5,000,000 KRW)·`max_order_amount_usd`(1,000 USD)·`daily_order_limit_usd`(5,000 USD)·`allow_symbols`/`deny_symbols`(JSON 리스트)·`enforce_market_hours`(True)·`paper_starting_cash`(**`{"KRW":"10000000"}` — 통화별 JSON dict**; 스칼라는 `{"KRW": …}` 래핑; float·bool 거부)·`confirmation_ttl_sec`(120, 상한 300 = `MAX_CONFIRMATION_TTL_SEC`: 토스가 `clientOrderId` 를 기억하는 10분을 넘겨 같은 토큰을 재시도하면 새 주문이 되는데 예약은 한 번만 센다. 실제 전송 시각은 `send_within` 이 막고, 상한은 보낼 시간이 거의 남지 않은 토큰을 받지 않기 위한 것이다)·`live_confirm_min_delay_sec`(0, off — live 환경 권장 5)·`audit_log_path`·**`state_backend`**(`memory`기본/`redis`)·**`redis_url`**(redis 백엔드 필수)·**`transport`**(`stdio`기본/`http`)·**`http_host`**(`127.0.0.1`)·**`http_port`**(`8000`)·**`auth_token`**(http 트랜스포트 필수). 돈 필드(`max_order_amount`·`daily_order_limit`·`max_order_amount_usd`·`daily_order_limit_usd`)는 `_no_float` validator 로 float 거부. `paper_starting_cash` 는 `_parse_starting_cash` validator 로 JSON dict·scalar·float·bool 처리(float/bool 거부). 사용자용 표는 `pytossinvest-mcp/README.md`.

## 이력

- 2026-11-12 전송 시한 추가(`send_within`, 0.0.3): 같은 토큰 재시도가 토스의 10분 멱등 창 밖에서 전송되지 않는다. 그전엔 429 백오프가 길게 이어지면 재시도가 새 주문으로 접수될 수 있었다.
- 2026-11-12 redis spend 카운터를 TTL 락에서 `WATCH/MULTI` 로 변경. 예약을 감사로그에 선기록하고 부팅 복원이 서 있는 예약을 모두 세도록 변경. 성공 뒤 기록 실패는 `warning`. redis 백엔드에서 액세스 토큰 공유(`RedisTokenCache`) 추가 — 2026-06-19 의 보류 결정을 토큰에 한해 뒤집음(레이트리미터는 그대로 보류). `confirmation_ttl_sec` 상한을 600초에서 300초로 낮춤. paper 도 재시도 실패와 스토어 오류에서는 예약 유지.
- 2026-11-12 병렬 툴콜 대응: `executing(token)` 실행권, memory 스토어·paper 스토어 락. 예약 해제 규칙 강화(`409 request-in-progress`, 체결 불명 뒤의 재시도 거부, 읽지 못한 200 응답은 예약 유지). 일반 정정의 감액 환급 제거(증가분만 계산). redis-py 예외도 `state-unavailable` 로 변환. `confirmation_ttl_sec` 상한 600초. `MemoryPaperStore.load()` 는 스냅샷. 그전엔 타임아웃 후 재시도가 `request-in-progress` 를 받으면 예약이 풀려, 첫 요청이 체결돼도 일일 한도에 잡히지 않았다. `TOSSINVEST_ACCOUNT_SEQ` 추가.
- 2026-09-30 조건주문 5종을 같은 안전모델로 노출.
