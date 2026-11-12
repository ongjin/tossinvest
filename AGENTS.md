# CRITICAL RULES

- **AI 작성 표시 금지**: 커밋 메시지·코드 주석·문서 어디에도 AI 가 작성했다는 내용을 넣지 않는다 (`Co-Authored-By: Claude`, "Generated with AI" 등 전부 금지). **공개 OSS 레포**라 더 엄격히.
- **커밋/푸시/머지는 요청 시에만**. 브랜치 전략은 `main` 단일. feature 작업은 `feat/<name>` 브랜치 → 리뷰 후 머지.
- **돈/수량 float 금지** — 전구간 **문자열/Decimal**. SDK 의 `pytossinvest.money.to_decimal` 이 float 를 `TypeError` 로 거부(강제). MCP 레이어도 입출력 모두 문자열 유지(JSON/Decimal 안전).
- **SDK 공개 API 깨지 말 것** — `pytossinvest-mcp` 가 `pytossinvest` 에 의존. SDK 시그니처/반환 타입 변경 시 **MCP 테스트도 그린 확인** 후 진행.
- **`place_order` 안전 불변식 (프로젝트 핵심)** — 체결 경로(`paper.place` / `client.place_order`)는 **반드시 `safety.check_guardrails` 를 거친다**. confirmation 토큰은 `preview_order` 에서만, 가드레일 통과 후 발급. 이 불변식을 우회하는 변경 금지.
  - **place 순서**: `executing(token)`(토큰당 실행은 한 번에 하나. 겹친 호출은 `confirmation-in-progress` 로 거부) 안에서 `consume(token)` → `check_guardrails(check_daily=False)` → **`reserve`(원자적·`clientOrderId` 멱등성)** → 감사로그에 `reserved` 선기록(재시작 복원용. 못 쓰면 보내지 않는다) → 실행 → **성공 시 `commit`(일일 누적 확정) / 실패 시 `release`(예약 해제, 토큰은 유지)** → 실패 시 동일 `clientOrderId` 로 멱등 재시도(유지된 예약은 dedup 키로 재사용돼 두 번 세지 않음). 전송은 preview 후 570초 안에만 한다(`send_within`): 토스의 멱등 창 10분을 넘긴 재전송은 새 주문이 된다.
  - **release 는 첫 시도의, 체결이 확실히 안 된 실패만**: paper 의 실패(paper 스토어 I/O 오류는 제외. redis 에는 체결이 저장됐을 수 있다), 그리고 live 의 토스 4xx(`409 request-in-progress` 는 제외. 같은 `clientOrderId` 가 아직 처리 중이라는 뜻이라 체결될 수 있다). 타임아웃·연결끊김·5xx 처럼 체결 여부를 모르면 예약을 유지한다. **그렇게 유지된 예약은 이후 재시도가 어떤 이유로 거부돼도 풀지 않는다**(401·429 는 주문 처리까지 가지 못한 응답이고, 정정 재시도의 "이미 체결"은 첫 정정을 되돌리지 않는다). 성공한 재시도만 예약을 정리한다. 브로커 호출이 성공한 뒤의 `commit`·감사 기록 실패는 예외가 아니라 `warning` 이 붙은 결과로 돌려준다(오류로 보이면 호출자가 새 preview 로 같은 주문을 또 낸다).
  - **modify 도 동형 2단계**(`preview_modify`→`modify_order(confirmation_token)`): `executing(token)` 안에서 consume → 가드레일 재검사(**델타 회계** — `check_daily=True, prev_notional=원본명목`) → **`reserve(delta)`** → 실행 → 성공 시 **`commit`** / 실패 시 place 와 같은 규칙으로 `release(delta)`(토큰 유지). **델타는 증가분만**(`spend_delta` = `max(0, new−old)`): 줄이는 정정은 취소와 마찬가지로 한도를 환급하지 않는다. 환급하면 MCP 가 센 적 없는 주문(앱에서 낸 주문)을 줄여 한도를 다시 쓰는 우회가 생긴다.
  - **조건주문도 같은 경로**: `preview_conditional_order`/`preview_conditional_modify` 가 `kind="conditional"` 토큰을 발급하고 실행은 기존 `place_order`/`modify_order` 가 **예약 이후 실행 단계에서만** `kind` 로 분기(`conditional.execute_place/execute_modify`) — 조건주문 전용 체결 경로를 따로 만들지 말 것.
  - **토큰 용도 검사**: `place_order` 는 정정 토큰(`modify_order_id` 있음)을, `modify_order` 는 주문 토큰을 `wrong-token` 으로 거부한다 — 정정 토큰은 차액만 예약하므로 `place_order` 로 새 주문을 내면 일일 한도를 우회한다(2026-09-30 리뷰에서 발견, 일반 주문에도 있던 구멍).

# 토스증권 Open API 오픈소스 (pytossinvest + pytossinvest-mcp)

토스증권 Open API 를 AI/퀀트에 연결하는 **오픈소스**. 1순위 목표는 **평판/포트폴리오**(수익은 스폰서·콘텐츠 부차). 차별점 = "어떻게 LLM 에게 진짜 증권계좌 키를 **안전하게** 쥐여주나"(안전모델). API 는 WTS `설정 > Open API` 에서 키 발급 가능(한도·정책 변동 가능)이지만, 라이브 키 없이 전부 만들고·테스트·데모 가능하게 설계됨(paper 모드 + mock fixture).

## Tech Stack

- **Runtime**: Python 3.12, **uv 워크스페이스** 모노레포 (hatchling build)
- **`pytossinvest`** (SDK, **MIT**, PyPI 배포 이름 `pytossinvest-sdk` — `--package` 에도 이 이름): `httpx`(sync) + `pydantic` v2. 토큰매니저·그룹별 레이트리미터·decimal-safe money·code 기반 에러·32 엔드포인트(API 1.2.19 REST 전부 — 조건주문 5·수급 5·랭킹·시장지표 3·전종목 포함) + 실시간 스트림 `pytossinvest.stream`(선택 의존성 `websockets`, `[ws]` extra).
- **`pytossinvest-mcp`** (MCP 서버, **Apache-2.0**, SDK 의존): `mcp>=2.0.0,<3`(lock 2.2.0, SDK v2 `MCPServer` — 2026-07-28 스펙, stdio) + `pydantic-settings`. 안전모델(모드·가드레일·preview/confirm·멱등성·감사로그) + 24 툴(조건주문 5·시장데이터 4 포함).
- **테스트**: `pytest`. SDK 는 `respx` 로 httpx mock, MCP 는 `FakeClient` + paper 엔진 — **라이브 키 불필요, 네트워크 0**.

## Project Structure

```
toss/
├── pyproject.toml                 # uv workspace: members = ["pytossinvest", "pytossinvest-mcp"]
├── pytossinvest/                  # SDK (MIT)
│   └── src/pytossinvest/          # money · errors · ratelimit · auth · models · client
├── pytossinvest-mcp/                # MCP 서버 (Apache-2.0)
│   └── src/pytossinvest_mcp/        # config · audit · paper · market_hours · safety · tools · server
├── examples/                       # 키 없이 실행되는 데모 (sdk_quickstart · mcp_paper_demo)
└── docs/
    ├── wiki/                                  # ★ living 레퍼런스 (토스 API · SDK · MCP — 코드 손대기 전 읽기)
    └── superpowers/specs|plans/               # 설계 스펙 + 구현 플랜 (Plan 1 SDK / Plan 2 MCP)
```

## Commands

```bash
# 의존성 동기화 (패키지별)
uv sync --package pytossinvest-mcp --extra dev

# 테스트
uv run --package pytossinvest-sdk --extra dev pytest pytossinvest/tests   # SDK (155) — respx mock + 로컬 websockets 서버
uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests           # MCP (310) — FakeClient
uv run --all-packages --all-extras pytest                                 # 두 스위트를 루트에서 한 번에

# MCP 서버 실행 (stdio — Claude Desktop/Cursor 등 MCP 클라이언트용)
TOSSINVEST_MODE=paper TOSSINVEST_CLIENT_ID=... TOSSINVEST_CLIENT_SECRET=... \
  uv run --package pytossinvest-mcp pytossinvest-mcp
```

## Conventions

- **money/quantity**: 전부 문자열/Decimal. float 진입 경로 자체를 안 만든다. (위 CRITICAL RULES)
- **SDK 규약** (`pytossinvest`): 응답 `result` 자동 언래핑(토큰 엔드포인트 제외)·**`code` 기반 에러 분기**(unknown code 관용)·**`X-RateLimit-*` 헤더가 진실**(표 숫자 하드코딩 금지)·`accountSeq` 1회 캐싱(ACCOUNT 1/s — 계좌헤더가 필요한 첫 호출이 알아서 `get_accounts()` 로 채움. 2026-09-30 전엔 `RuntimeError` 로 먼저 부르라고 강제했는데, mcp 2.1+ 에선 그 안내가 모델에 가려져 계좌 툴이 막혔다)·**`clientOrderId` 멱등성 수동**(10분). v0.0.2: `X-RateLimit-*` 헤더로 버킷 동적 동기화(헤더가 진실 — 본 그룹은 피크반토막 미적용), 429 **bounded 자동 retry**(`Retry-After` 또는 지수백오프+jitter, `max_retries` 기본 3, `retry_max_wait` 60s 상한) 구현. **5xx·타임아웃은 비재시도**(호출자 책임). 소진 시 종전대로 `RateLimitError` throw. **401 은 `expired-token`·`token-revoked` 일 때 토큰을 버리고 최대 2회 재발급**(둘째 전에 짧은 jitter), `token_cache` 로 프로세스 간 토큰 공유 가능 — 토스는 client 당 유효 토큰이 1개라 같은 키로 다른 프로세스(예: 로컬 Docker 와 OCI)가 발급하면 우리 토큰이 revoke 된다(2026-09-30 전엔 `expired-token` 만 재시도해 캐시 만료까지 401 이 이어질 수 있었다). 생성자 `account_seq` 로 계좌 지정(생략 시 첫 계좌). `to_decimal` 은 `NaN`·`Infinity` 도 거부(`InvalidOperation`).
- **MCP 안전모델** (`pytossinvest-mcp`): 3모드 `TOSSINVEST_MODE` = `read_only`(주문툴 미등록) / **`paper`**(기본, `PaperBroker` 체결 — `PaperStore` 백엔드: memory 로컬 / redis 공유) / `live`(`TOSSINVEST_ALLOW_LIVE=1` 까지 있어야 켜짐 — config validator 가 이중게이트). 가드레일(**주문통화별** 주문당·일일 상한·allow/deny(**취소에도 적용** — `check_symbol`, 조건주문은 사용자 손절일 수 있어서)·고액 confirm 필수·하드실링 거부 — KRW 1억/30억, USD $10만/$300만, 시세 응답 통화 우선·없으면 숫자로 시작=KRW·그 외=USD·FX 환산 X; 장시간 게이트는 live 전용). preview→place / preview_modify→modify 2단계 + **reserve-first 멱등성**(시도 시 원자적 예약 → 실패 시 release → 성공 시 commit; modify 도 증가분 delta 로 동형, 감액 환급 없음) + place 시 일일한도 재검사 + 부팅 시 감사로그로 당일 누적 복원 + 감사로그(JSONL). **상태 백엔드**: `TOSSINVEST_STATE_BACKEND`=`memory`(기본·단일인스턴스) / `redis`(`TOSSINVEST_REDIS_URL` 필요·HA) — `TokenStore`/`SpendStore`/`PaperStore` 심을 통해 교체 가능. redis 시 token/spend counter 가 진실의 원천(AOF), 부팅 복원 no-op. redis 백엔드는 토스 액세스 토큰도 인스턴스끼리 공유한다(`RedisTokenCache`, client secret 에서 유도한 키로 암호화). **paper 상태도 redis 로 외부화** — `RedisPaperStore`가 단일 JSON 키 `paper`에 저장(`lock:paper` 분산락), 다중 인스턴스 공유·재시작 생존. **트랜스포트 축**: `TOSSINVEST_TRANSPORT`=`stdio`(기본·무변경, `mcp.run()`) / `http`(원격, bearer 인증 필수 — `TOSSINVEST_AUTH_TOKEN` 없이 부팅 거부). mode/state_backend 와 독립(직교).
- **설정**: `pytossinvest-mcp` 는 `pydantic-settings`, env prefix `TOSSINVEST_` (`MODE`/`ALLOW_LIVE`/`CLIENT_ID`/`CLIENT_SECRET`/`ACCOUNT_SEQ`/`MAX_ORDER_AMOUNT`/`DAILY_ORDER_LIMIT`/`ALLOW_SYMBOLS`/`DENY_SYMBOLS`/`ENFORCE_MARKET_HOURS`/`MAX_ORDER_AMOUNT_USD`/`DAILY_ORDER_LIMIT_USD`/`LIVE_CONFIRM_MIN_DELAY_SEC`/`PAPER_STARTING_CASH`/`STATE_BACKEND`/`REDIS_URL`/`TRANSPORT`/`HTTP_HOST`/`HTTP_PORT`/`AUTH_TOKEN`/`HTTP_ALLOWED_HOSTS`). `PAPER_STARTING_CASH` 는 **통화별 JSON dict**(`{"KRW":"10000000","USD":"7000"}`) 또는 스칼라(`10000000` → `{"KRW":…}` 래핑); float·bool 거부. 상세는 `pytossinvest-mcp/README.md`.
- **라이선스**: SDK=**MIT**, MCP=**Apache-2.0**. 각 패키지에 `LICENSE`(+MCP `NOTICE`) + `pyproject.toml` `license` 필드. README 에 명시. "비공식(unofficial) 클라이언트" 표기로 토스 상표/엔도르스먼트 오해 방지.

## 주의할 함정 (이미 겪었거나 설계로 막은 것)

- **토스 API 정책 변동 가능** — 한도·정책·엔드포인트·필드가 사전 공지 없이 바뀔 수 있다. `docs/wiki/tossinvest-open-api.md` 는 **스냅샷**이니 막히면 canonical `openapi.json` 재확인(자가갱신).
- **paper modify/cancel 은 live 전용** — paper 는 즉시체결 모델이라 정정/취소할 미체결 주문이 없음(`PaperError` 로 명확히 거부, 실제 `409 already-filled` 미러링).
- **paper MARKET 체결가** — 체결 시점에 시세가 비면 가격 0 으로 조용히 체결되던 버그를 막음 → ref price 없으면 `PaperError`(토큰 살려둠, 재시도 가능).
- **notional 통화** — 가드레일은 주문통화 기준 비교(FX 환산 X). 임계는 통화별 — KRW 1억/30억, USD $10만/$300만.
- **market_hours US 자정넘김** — 미국장을 KST 로 표기하면 23:30→06:00 처럼 자정을 넘긴다. `start > end` 면 wrap 윈도우(`now >= start or now < end`)로 처리. 깨진 시간 문자열은 "닫힘"으로 안전 처리.
- **MCP 테스트 import** — 테스트에서 `conftest` 의 `FakeClient` 는 `from conftest import FakeClient`(pytest 가 tests/ 를 sys.path 에 넣음). `from tests.conftest` 는 `tests` 패키지가 없어 깨진다.
- **통화 판정**(M1·C1 후속 반영): preview(`preview_order`/`preview_modify`)가 `get_prices([symbol])` 한 번으로 **권위 통화**(`Price.currency`)를 얻어 `build_spec(currency=…)` 로 주입; 조회 실패/빈결과/공백통화는 `order_currency(symbol)`(숫자로 시작=KRW — KR 코드는 영숫자 `0101N0` 도 있음 — 그 외=USD) **폴백**. 폴백은 한도가 작은 USD 쪽으로 떨어지게 짰다 — 예전 `isalpha()` 폴백은 `BRK.B` 를 KRW 로 봐 주문당 $1,000 대신 1,000,000 을 한도로 쓰는 구멍이었다(2026-09-30 수정). notional 단위는 주문통화, FX 환산 없음. KRW/USD 버킷 분리 유지. 이 권위 통화는 **장시간 게이트 국가 판정**(`_market_gate`→`_country_for_order`: USD→US·KRW→KR, 없으면 같은 `order_currency` 폴백)에도 재사용 — 가드레일 통화와 미/한국장 판정이 한 소스.
- **modify 델타 회계(M1)** — `preview_modify` 가 원본 주문 명목(`get_order` 의 price×qty)을 `spec.prev_notional` 로 잡고, 일일 버킷에는 **증가분만**(`max(0, new−old)`) 검사·가산한다. per-order/고액/하드실링은 여전히 전액. **줄이는 정정은 환급 없음**(2026-11-12 전엔 음수 델타를 credit 으로 반영해, 앱에서 낸 주문을 줄이면 MCP 누계가 내려가 한도 100만에 150만이 나갔다. 0-하한으로는 막히지 않는다). 스토어의 0-하한은 그대로 남아 있지만 `SafetyManager` 는 더 이상 음수 델타를 넘기지 않는다.
- **_spent 부팅 복원(memory 백엔드)** — 서버 시작 시 `audit.read_events()`→`safety.restore_spend` 가 당일(UTC ts→KST 날짜) 감사 이벤트로 누계를 되살린다. 아직 서 있는 예약을 모두 센다: 보내기 전에 쓴 `reserved` 와 `placed`·`modified` 를 `clientOrderId` 당 한 번, `reservation: released` 로 끝난 것은 제외. 그래서 체결 불명으로 끝났거나 호출 도중 프로세스가 죽은 주문도 재시작 뒤 한도에 남는다. 감사 파일을 지우면 당일 누적도 리셋된다. redis 백엔드는 counter 가 진실의 원천(AOF)이라 복원이 no-op 이다.
- **mcp SDK ≥2.1 은 도구 예외 텍스트를 숨긴다** — `ToolError` 가 아닌 예외는 모델에 `Error executing tool <name>` 만 간다. 그래서 `server.py` 는 `@mcp.tool` 대신 **`@mcp_tool`**(`_model_facing_tool`)로 등록해 `GuardrailError`·`PaperError`·`TossInvestError`·`ValueError`·`httpx.HTTPError`(타임아웃 — 체결 불명 실패 뒤 같은 토큰 재시도 판단용) 를 `ToolError` 로 바꿔 올린다. 새 툴도 `@mcp_tool` 로 — 안 그러면 한도 거부 사유가 모델에 안 보인다.
- **툴은 워커 스레드에서 병렬로 돈다**: mcp SDK 가 sync 툴을 `anyio.to_thread` 로 실행해서, 병렬 툴콜이면 `app.client`·스토어·paper 를 동시에 쓴다. 가변 상태를 더하면 락을 같이 건다. 상세: `docs/wiki/pytossinvest-sdk.md` '스레드 공유', `docs/wiki/pytossinvest-mcp.md` '모듈별 함정'.
- **SDK 경로 값은 `_path_seg()` 로**: 주문 id·심볼처럼 URL 경로에 넣는 값은 LLM 이 채운다. 그대로 넣으면 `../holdings` 같은 값이 다른 엔드포인트로 풀린다. 상세: `docs/wiki/pytossinvest-sdk.md` '경로에 들어가는 값'.
- **주문 파라미터는 스키마 enum** — `server.py` 의 `Side`·`OrderType`·`TimeInForce`(`DAY|CLS|OPG`)·`OrderStatus`(`OPEN|CLOSED`)·`Currency` `Literal` 별칭. 이 모듈은 `from __future__ import annotations` 라 **별칭을 따옴표로 한 번 더 감싸면 안 된다**(`"Currency | None"` 는 이중 문자열이 돼 SDK 가 pydantic 모델을 못 만든다).
- **확인 토큰 만료는 벽시계(`time.time`)** — 토큰이 redis 에 저장되므로 `time.monotonic` 을 쓰면 호스트(OrbStack VM 포함) 재시작·다중 호스트에서 만료 판정이 틀어진다(2026-09-30 수정).
- **Round 2 하드닝 추가 사항** — `build_spec` 에서 `order_amount` 를 `price` 또는 `quantity` 와 같이 전달하면 `invalid-order-params` 에러(동시 전달 금지). deny/allow 심볼 매칭은 양쪽을 `.strip().upper()` 정규화하여 대소문자·앞뒤 공백 무시(단, `spec.symbol` 자체는 변경 안 함 — 브로커로 원본 전달). SDK 200 경로가 `(ValueError, RecursionError)` 모두 `invalid-response` 로 처리(깊이 중첩된 JSON 이 부팅 크래시 내지 않음).
- **상태 백엔드 심(seam) — memory|redis + reserve-first** — `TokenStore`/`SpendStore`/`PaperStore` 인터페이스로 백엔드 교체. place 흐름: `reserve`(원자적, `clientOrderId` 멱등) → 실행 → 성공 `commit` / 실패 `release`. modify 도 `reserve(delta)` → `commit/release` 동형(delta 는 증가분만). 백엔드 선택은 `TOSSINVEST_STATE_BACKEND`(`memory`/`redis`).
- **`_guard_store` fail-closed** — store I/O 중 `ConnectionError`/`Timeout`/`OSError` 와 redis-py 의 `RedisError`(내장 예외를 상속하지 않는다)는 `GuardrailError("state-unavailable")`로 변환(fail-closed). `GuardrailError` 자체는 절대 삼키지 않는다.
- **redis 일일 카운터는 Decimal 문자열 + `WATCH/MULTI` 트랜잭션** — 금액은 Python Decimal 로 계산하고, 읽은 뒤 키가 바뀌었으면 통째로 재시도한다. TTL 락은 쓰지 않는다(락을 쥔 쪽이 TTL 보다 오래 멈추면 다른 인스턴스의 예약을 덮어쓴다). `INCR`/`INCRBYFLOAT` 금지(돈/수량 float 규칙 동일 적용).
- **`fakeredis[lua]` 필수** — redis-py `Lock.release()`는 내부적으로 EVALSHA(Lua)를 사용. fakeredis 의 lua 서브패키지가 없으면 Lock.release()에서 `ResponseError` 발생. 실제 Redis 에는 Lua가 기본 내장.
- **PaperStore 심 — memory|redis + 통화별 현금 버킷** — `PaperBroker` 는 `PaperStore`(`lock()/load()/save()`)로 상태를 읽고 쓴다. 현금·실현손익은 통화별 버킷(FX 환산 없음), `place()` 는 `clientOrderId` 멱등. 상세: `docs/wiki/pytossinvest-mcp.md` 'PaperStore 심'.
- **http 트랜스포트 함정** — `TOSSINVEST_TRANSPORT=http` 로 부팅 시 `TOSSINVEST_AUTH_TOKEN` 미설정이면 config validator 가 `ValueError`로 거부(live/redis 이중게이트와 동형 삼중 fail-closed). bearer 는 `hmac.compare_digest` 상수시간 비교(타이밍 공격 방지) — **bytes 로 비교**(str 비교는 비ASCII 헤더에서 `TypeError` → 500). MCP 엔드포인트는 `/mcp`(Streamable HTTP, `stateless_http=True` — **SDK v2 는 이걸 생성자가 아니라 `streamable_http_app()` 인자로 받는다**, `server.transport_kwargs(settings)` 가 단일 seam). uvicorn 은 옵션 `[http]` extra — `serve_http` 안에서만 import(stdio 설치·테스트 스위트는 uvicorn 불필요). 단일테넌트 — 토큰은 엔드포인트 인증일 뿐, Redis 미저장·Toss 자격증명 아님. **DNS-rebinding**: `transport_kwargs` 는 http 일 때 SDK 의 localhost-only 자동보호(비-localhost `Host`→421)를 끈다(bearer 가 인증면, 배포 host 는 프록시 제어) — 단 `TOSSINVEST_HTTP_ALLOWED_HOSTS`(JSON 리스트, 기본 빈값)를 주면 그 목록으로 Host 핀닝 재활성(심화방어, 미허용 Host→421). stdio 는 인자 미전달로 무변경.

## 추가 문서 (docs/)

특정 작업 들어갈 때 아래를 직접 읽어와서 참고. `docs/wiki/*` = **지금 어떻게 동작하나 / 어떻게 작업하나**(living, 자가갱신), `docs/superpowers/*` = **설계 시점 기록**(spec·plan, 고정).

**living (docs/wiki/ — 코드 만지기 전 읽고, 만진 뒤 갱신):**
- [docs/wiki/tossinvest-open-api.md](docs/wiki/tossinvest-open-api.md) — **토스 Open API 레퍼런스 (외부 스펙)**. 인증 2단(`X-Tossinvest-Account`)·엔드포인트 전체·요청/응답 스키마·enum·rate limit 10그룹·에러코드 전체표·주문 함정(멱등성 10분·고액확인·US 금액주문·OrderStatus 10종). 외부 API 사실관계가 필요할 때.
- [docs/wiki/pytossinvest-sdk.md](docs/wiki/pytossinvest-sdk.md) — **SDK 내부구조**. 공개 API 표면(클라이언트·에러·모델·money), `_request` 오케스트레이션(언래핑·계좌헤더·401재시도·레이트게이트·헤더동기화·429재시도), 모듈별 책임·함정·레이트리밋/재시도 동작(v0.0.2), 새 엔드포인트 추가 절차. `pytossinvest/` 코드 만질 때.
- [docs/wiki/deploy.md](docs/wiki/deploy.md) — **서버 배포**. `deploy/remote.sh` 로 배포·롤백. 서버에 올리거나 되돌릴 때.
- [docs/wiki/release.md](docs/wiki/release.md) — **PyPI 릴리스 절차**. 버전 올리기, 태그 이름, Trusted Publishing 워크플로. 새 버전을 낼 때.
- [docs/wiki/pytossinvest-mcp.md](docs/wiki/pytossinvest-mcp.md) — **MCP 내부구조 + 안전 불변식**. 3모드 라우팅표, 가드레일 순서(통화별 임계), preview→place/modify 토큰 생애·멱등성, 24툴(조건주문·시장데이터), 모듈별 함정(paper 즉시체결·MARKET 무가격·US 자정넘김·conftest import·통화판정·M1 modify·부팅복원), 새 툴 추가 절차. `pytossinvest-mcp/` 코드 만질 때.

**design history (docs/superpowers/ — 왜 이렇게 만들었나):**
- [docs/superpowers/specs/2026-06-17-tossinvest-mcp-design.md](docs/superpowers/specs/2026-06-17-tossinvest-mcp-design.md) — **설계 확정본**. 모드 3단계·안전모델(§3)·툴 매핑(§4)·크로스커팅 인프라(§5)·테스트 전략(§6)·규제 메모(§7)·라이선스(§9).
- [docs/superpowers/plans/2026-06-17-pytossinvest-sdk.md](docs/superpowers/plans/2026-06-17-pytossinvest-sdk.md) — **SDK 구현 플랜 (Plan 1)**. TDD 태스크별 코드.
- [docs/superpowers/plans/2026-06-17-tossinvest-mcp-server.md](docs/superpowers/plans/2026-06-17-tossinvest-mcp-server.md) — **MCP 구현 플랜 (Plan 2)**. 모듈별 TDD 태스크·안전 불변식·테스트.

## 🔄 문서 자가갱신 (이 AGENTS.md 포함 **모든** 상세문서 — 꼭 지킬 것)

- **작업 끝나면 동기화 (별도 요청 없이)**: 의미 있는 변경(새 모듈·엔드포인트·MCP 툴·모드·가드레일·새 함정·새 컨벤션/환경변수)은 **같은 세션에서** **이 `AGENTS.md` + 관련 `docs/wiki/*` 문서**를 갱신한다. **커밋/푸시는 수동** (문서 갱신 ≠ 커밋). 자잘한 버그픽스·일회성 작업은 제외.
- **자가갱신 대상 = AGENTS.md + docs/wiki/ 전부** (`docs/superpowers/*` 는 설계 시점 기록이라 고정 — 갱신 대상 아님). 무엇을 만졌으면 무엇을 갱신하나:
  - SDK(`pytossinvest/`) → [docs/wiki/pytossinvest-sdk.md](docs/wiki/pytossinvest-sdk.md) (+ 공개 API 바뀌면 이 AGENTS.md Conventions/함정)
  - MCP(`pytossinvest-mcp/`) → [docs/wiki/pytossinvest-mcp.md](docs/wiki/pytossinvest-mcp.md) (+ 모드·불변식·env 바뀌면 이 AGENTS.md)
  - 외부 토스 API 사실관계 → [docs/wiki/tossinvest-open-api.md](docs/wiki/tossinvest-open-api.md)
  - 각 `docs/wiki/*` 문서 상단엔 자체 `🔄 자가갱신` 문구가 있다(스냅샷·코드가 진실). 코드와 어긋난 걸 발견하면 그 자리에서 고친다.
- **토스 API 레퍼런스는 스냅샷**: 권위 순서 ① canonical `openapi.json` → ② developers.tossinvest.com/docs → ③ 본 문서. 코드 작업 중 불일치 발견 시 즉시 그 문서 갱신.
- **AGENTS.md 는 high-signal 만** (CRITICAL RULES·Tech Stack·Structure·Commands·Conventions·함정·docs 인덱스). 한 주제가 길어지고 *특정 작업 시에만* 필요하면 `docs/wiki/<topic>.md` 로 분할하고 본문엔 인덱스 한 줄만. 인덱스 hook(언제 읽나)이 부정확해지면 같이 고친다.
