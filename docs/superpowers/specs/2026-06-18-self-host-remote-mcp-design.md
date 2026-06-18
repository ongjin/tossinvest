# 셀프호스트 원격 MCP — 설계 문서

> **상태**: 설계 확정 (구현 전). Phase 1 = 원격 트랜스포트 + 엔드포인트 인증 + Redis HA 상태 외부화.
> **대상 패키지**: `pytossinvest-mcp` (Apache-2.0). SDK(`pytossinvest`) 공개 API 변경 없음.
> **선행 문서**: [2026-06-17-tossinvest-mcp-design.md](2026-06-17-tossinvest-mcp-design.md) (안전모델 원본), [2026-06-17-mcp-safety-accounting-design.md](2026-06-17-mcp-safety-accounting-design.md) (modify 델타·부팅복원).

---

## 1. 목표와 배경

### 1.1 무엇을, 왜
현재 `pytossinvest-mcp` 은 **stdio 단일 프로세스**다 — AI 클라이언트(Claude Desktop 등)와 같은 기계에서만 동작. 이를 **원격 배포 가능한 MCP 서버**로 확장한다: 유저가 클라우드/서버에 띄우고 AI 클라이언트가 원격 접속.

### 1.2 핵심 제약 — 토스 인증이 위임(delegation)을 안 준다 (설계를 가른 사실)
토스 Open API 는 **OAuth 2.0 Client Credentials** 다 (`grant_type=client_credentials`). 각 토스증권 계좌 보유자가 **본인 WTS `설정 > Open API` 에서 `client_id`/`client_secret` 를 직접 발급**한다. end-user OAuth 위임 흐름(authorization_code)이 **없다** — 발급되는 건 그 사람 계좌의 장수(long-lived) 풀액세스 secret 하나뿐.

**귀결**: 멀티테넌트 호스팅은 곧 *모든 유저의 raw 증권 secret 데이터베이스*가 된다는 뜻 — 회수·스코프·만료 안전장치 없이. 프로젝트 명제("어떻게 LLM 에게 키를 **안전하게** 쥐여주나")를 정면으로 거스르고, ToS·금융규제 게이트까지 걸린다. **그래서 위임이 없는 환경에서 유일하게 방어 가능한 설계 = secret 이 유저 인프라를 떠나지 않는 것 = 셀프호스트 단일테넌트.**

### 1.3 범위 (Phase 1)
- **원격 트랜스포트**: Streamable HTTP (기존 stdio 와 **선택제** 공존, stdio 무변경).
- **엔드포인트 인증**: http 모드에서 bearer 토큰(노출된 엔드포인트는 URL 아는 누구나 주문 가능 → 필수).
- **Redis HA 상태 외부화**: confirmation 토큰·일일캡·paper 상태를 공유 Redis 로 → N 인스턴스 LB 뒤 HA / 재시작 내성.
- **단일테넌트**: 자격증명 하나(유저 키, config). Redis 엔 *상태만*, secret 은 안 들어감.

### 1.4 비목표 (Non-goals, YAGNI)
- **멀티테넌트 / 키 보관(custody)** — §1.2 사유로 명시적 제외. 호스티드 SaaS 로 피벗하려면 별도 스펙 + ToS/규제 선결.
- **인스턴스 간 공유 레이트리미터** — Phase 1 은 SDK 의 헤더동기화 + 429 bounded retry 로 버팀(테넌트=유저 1명이라 토스 한도도 그의 것). 공유 토큰버킷은 후속.
- **MCP OAuth 2.1 인증 프레임워크** — 단일테넌트 자가배포엔 과함. 설정형 정적 bearer 로 충분.
- **paper 모드 제거** — 유지(유저가 라이브 키 없이 체험). Redis 로 외부화.

### 1.5 왜 HA 까지? (의사결정 기록)
단일유저 볼륨엔 수평확장이 거의 무의미하다고 검토됨. 그럼에도 **재시작 내성 + 다중 인스턴스 가용성**을 위해 상태 외부화를 Phase 1 에 포함하기로 사용자가 결정. memory 백엔드(현재 동작)는 stdio·테스트 기본으로 보존되므로 추가 비용은 redis 백엔드 구현에 국한.

---

## 2. 아키텍처

```
   AI client ──▶ Load balancer (HA: N 인스턴스)
                      │  Streamable HTTP + Bearer 인증
            ┌─────────▼──────────┐   ┌──────────────┐
            │  pytossinvest-mcp   │   │  인스턴스 #2…N │
            │  AuthMiddleware     │   │  (동일)       │
            │  FastMCP tools(14)  │   └──────┬───────┘
            │  SafetyMgr│Paper    │          │
            │  (store seam)       │          │
            └───────┬─────────────┘          │
                    │                         │
            ┌───────▼─────────────────────────▼───┐
            │           Redis (공유 상태)           │
            │ tok:* (TTL) · spend:* (Lua 원자)      │
            │ paper:* (락+dedup) · audit (stream)   │
            └───────────────────────────────────────┘
                    │ 단일 자격증명 (유저 키, 인프라 밖으로 안 나감)
            ┌───────▼────────┐
            │  토스 Open API  │  ← SDK: 헤더동기화 + 429 retry
            └────────────────┘
```

**5가지 핵심 결정:**
1. **트랜스포트 선택제** — `TOSSINVEST_TRANSPORT=stdio|http`. stdio 경로 보존. http 면 ASGI(uvicorn) + Streamable HTTP + `stateless_http=True`.
2. **엔드포인트 인증** — http 모드만. `TOSSINVEST_AUTH_TOKEN` bearer 미들웨어. 토큰 없이 http 부팅 거부.
3. **상태 백엔드 선택제** — `TOSSINVEST_STATE_BACKEND=memory|redis`. memory=현재 in-memory(기본·테스트). redis=HA. 진짜 두 백엔드가 필요해 생기는 **얇은 seam**(stdio 유저에게 Redis 강제 불가 + 테스트 네트워크 0). 무거운 멀티스토어 추상화 아님.
4. **안전 불변식 보존** — preview→place 2단계·consume-on-success·modify 델타회계가 Redis 위에서 **원자적으로** 재구현. 불변식 의미 보존, 구현만 분산안전.
5. **단일테넌트** — secret 은 Redis 에 안 들어감.

---

## 3. 컴포넌트 / 모듈 변경

| 모듈 | 변경 |
|---|---|
| `config.py` | + `transport`, `http_host/port`, `auth_token`, `state_backend`, `redis_url`. 검증: http⇒auth_token 필수, redis⇒redis_url 필수 (기존 `_live_requires_allow` 패턴) |
| `safety.py` | `SafetyManager` 정책 불변, 저장만 `TokenStore`/`SpendStore` seam 뒤로 (`_pending`·`_spent`) |
| `paper.py` | `PaperBroker` 체결 수학 불변, 상태(현금/포지션/주문)만 store 뒤로 |
| `audit.py` | + Redis stream 싱크(파일 싱크와 동형 인터페이스) |
| `tools.py` | **거의 무변경** — seam 이 툴 아래 (14툴 회귀위험 최소) |
| `server.py` | `build_app_context` 가 backend 별 store 조립 / `main` 이 transport 분기 + http auth 미들웨어 |
| **신규** `stores.py` | 프로토콜 + **memory 구현**(현재 dict 동작) |
| **신규** `redis_stores.py` | **redis 구현** (redis-py 내장 `Lock` 분산락 + Python `Decimal` RMW, 커스텀 Lua 없음). `redis` import 격리(옵션 의존성) |
| **신규** `http.py` | ASGI 조립 + bearer 미들웨어 |

**구현 결정:**
- ⚠️ **돈은 Redis 에서도 Decimal/문자열** (CRITICAL RULE 준수) — Redis 의 `INCR`/`INCRBYFLOAT` 는 long double 기반이라 **decimal-safe 하지 않다**. 일일캡 카운터는 돈이므로 **decimal 문자열로 저장**하고, 증감은 **분산락 안에서 Python `Decimal` read-modify-write** 로 한다. (paper 평단가·현금도 동일.)
- **일일캡 = reserve-first 로 통일** — 문서화된 "성공 시에만 finalize" 불변식을 **"시도 시 예약 / 실패 시 해제 / 성공 시 유지"** 로 정제(캡 강제 효과 동일, 분산안전 필수). 예약은 **(day,currency) 단위 Redis 분산락(redis-py `Lock`) 안에서 Decimal RMW** — `cur+delta≤cap` 이면 카운터=`str(cur+delta)`. memory 도 같은 의미로 통일(dict). **구현 시 CLAUDE.md 불변식 문구 갱신 대상.**
- **paper 도 동일 분산락** — place 를 (account 단위) Redis 락으로 감싸 Python 평단가 수학 그대로 유지. clientOrderId dedup 도 락 안에서.
- **커스텀 Lua 없음** — 원자성은 redis-py 내장 `Lock`(`SET NX PX` 기반, fakeredis 지원) 으로. 멱등 셋(`reserved:{day}`)의 SADD/SISMEMBER 는 돈이 아니라 그대로 사용.
- **redis 백엔드는 카운터가 진실의 원천** — AOF 내구+공유라 감사로그 리플레이 복원 불필요. `restore_spend` 는 memory 백엔드 전용. 감사 stream 은 신뢰/디버그용.
- **의존성**: `redis`(옵션 `[redis]`), `uvicorn`(옵션 `[http]`), 테스트 `fakeredis`(dev).

---

## 4. 데이터 플로우

**preview_order (STEP 1, 인스턴스 A)**
1. `get_prices([symbol])` → 권위 통화 (기존)
2. `build_spec(currency=…)` → notional
3. `check_guardrails`(일일 **읽기전용** 사전점검 — UX용 빠른 거절, 예약 아님)
4. `SET tok:{token} = serialize(spec) EX confirmation_ttl_sec` (TTL 네이티브)
5. return `{confirmation_token, 견적}`

토큰 값 = spec 전체(symbol/side/qty/price/notional/currency/clientOrderId/issued_at). issued_at 은 live-confirm-min-delay 검사용.

**place_order (STEP 2, 인스턴스 B — 토큰은 어느 인스턴스서든 소비)**
1. `spec = GET tok:{token}` (없음/만료 → invalid/expired-confirmation)
2. (live) `now - issued_at < delay` → confirm-too-soon
3. **reserve** (멱등, **(day,cur) 분산락 안 Decimal RMW**, §5 참조):
   ```
   with Lock("lock:spend:{day}:{cur}"):
       if SISMEMBER reserved:{day} clientOrderId: return OK          # 멱등
       cur = Decimal(GET spend:{day}:{cur} or "0")
       if cur + delta > cap: return REJECT(daily-limit)
       SET spend:{day}:{cur} = str(cur + delta)                       # decimal 문자열
       SADD reserved:{day} clientOrderId ; (TTL=KST 자정) ; return OK
   ```
4. 실행: `client.place_order(spec)` 또는 `paper.place(spec)`
   - 성공 → `DEL tok:{token}` (소비 확정, 예약 유지)
   - 실패 → release(−delta, `SREM`, 0-하한, 동일 락) ; 토큰 유지 → 멱등 재시도
5. audit `XADD(placed, notional, currency)`

⚠️ **예약(3)이 실행(4) 앞에 락으로 직렬화** — 두 인스턴스 동시 place 가 캡을 못 넘김. 실패 release, 크래시로 새도 캡이 *조여지는* 안전방향, 일일키 TTL 로 자정 자가치유.

**동시 중복 place** — 같은 토큰 A·B 동시 도달: live=토스 clientOrderId 10분 dedup / paper=account 단위 Redis 락 안 clientOrderId 검사 → 실제 1건.

**modify (델타 회계)** — 동형: `preview_modify` 가 원본명목(`get_order` price×qty)=prev_notional, 토큰 SET. `modify_order`: GET → reserve(delta=new−old) → 실행 → 성공 DEL / 실패 release. per-order·고액·하드실링은 전액, 일일캡만 증분. 음수 델타는 release 0-하한.

**memory 백엔드**: 위 락+RMW 자리를 단일 dict 연산으로(단일 인스턴스 레이스 없음). 의미 동일.

---

## 5. 에러 처리 / 장애 모드

**대원칙: fail-closed** — 상태 검사·예약 못 하면 주문 절대 통과 금지.

| 장애 | 처리 | 방향 |
|---|---|---|
| Redis 다운/연결실패 | preview·place 가 `state-unavailable` 거절 (가드레일 우회 금지) | fail-closed |
| 예약 후 크래시(release 못 함) | 예약 잔존 → 캡 조여짐, 일일키 TTL 자가치유 | 안전 |
| 토스 실행 실패 | release(−delta) + 토큰 유지 → 멱등 재시도 | 복구 |
| 실행 성공 후 DEL 실패 | 토큰 잔존(TTL). 재시도 시 멱등 예약으로 이중가산 차단 | — |
| 같은 토큰 동시 place | live=토스 dedup / paper=락+dedup | 1건 |
| http 인증 누락/오류 | auth_token 없이 부팅거부 / 잘못된 bearer 401 | fail-closed |
| 자정경계 시계차 | 인스턴스별 KST 날짜로 인접 버킷 분리(수초 창) — 허용 | 주석 |
| Redis 영속 유실(AOF off) | 카운터 리셋 위험 → **AOF 필수** 문서화, 감사 stream 2차 재구성원 | 운영 |

**멱등 예약 (부분실패 정확성)**: reserve 를 clientOrderId 키로 멱등화(분산락 안에서 `SISMEMBER reserved:{day}` 선검사 → 이미 있으면 재가산 X). release 동형(`SREM`+Decimal 차감, 0-하한). "실행 성공→DEL 실패→재시도" 가 정확히 1회만 반영.

---

## 6. 테스트 전략 (네트워크 0, 라이브 키 불필요)

- **회귀 가드**: 기존 MCP 112 / SDK 59 그대로 그린(memory 기본).
- **백엔드 패리티**: `backend`(memory|fakeredis) 파라미터라이즈로 동일 시나리오 관측결과 일치 → seam 이 의미 불변 증명.
- **분산 정확성(redis 신규)**:
  - reserve-first 캡 강제(캡 근처 둘째 reserve 거절, 락으로 직렬화)
  - 멱등 예약(같은 clientOrderId 재호출 1회만, release 동형)
  - **decimal 정밀도**(소수 notional 누적이 float 오차 없이 정확 — 카운터가 decimal 문자열)
  - 토큰 생애 HA(SafetyManager 둘이 같은 fakeredis 공유 → A preview / B place 성공, 실패 잔존, 성공 DEL)
  - paper 락+dedup(동시 clientOrderId → 1건)
  - modify 델타(증분만, 음수 0-하한)
  - fail-closed(Redis 끊김 주입 → `state-unavailable`)
- **트랜스포트/인증**: bearer 누락/오류 401·정상 통과 / config 검증(http⇒auth_token, redis⇒redis_url) / stdio 무변경 스모크.

✅ **커스텀 Lua 제거로 fakeredis 패리티 깔끔** — 원자성을 redis-py 내장 `Lock`(`SET NX PX`, fakeredis 지원)으로 하므로 단위테스트가 실제 Redis 없이 redis 경로를 그대로 검증. 실제 Redis 통합테스트는 선택(`@pytest.mark.integration`, CI 옵트인)으로 락 경합/AOF 만 확인.

---

## 7. 환경변수 (신규)

| 변수 | 기본 | 의미 |
|---|---|---|
| `TOSSINVEST_TRANSPORT` | `stdio` | `stdio` \| `http` |
| `TOSSINVEST_HTTP_HOST` | `127.0.0.1` | http 바인드 호스트 |
| `TOSSINVEST_HTTP_PORT` | `8000` | http 포트 |
| `TOSSINVEST_AUTH_TOKEN` | `""` | http 모드 bearer (http 면 필수) |
| `TOSSINVEST_STATE_BACKEND` | `memory` | `memory` \| `redis` |
| `TOSSINVEST_REDIS_URL` | `""` | redis 백엔드 접속 (redis 면 필수) |

---

## 8. 단계 (Phasing)

- **Phase 1 (이 스펙)**: 트랜스포트 선택제 + bearer 인증 + 상태 백엔드 seam(memory|redis) + redis 구현(reserve-first·멱등예약·paper 락·감사 stream) + 배포 템플릿(Docker compose: app + Redis) + 테스트.
- **Phase 2 (후속 스펙)**: 인스턴스 간 공유 레이트리미터(Redis 토큰버킷), 토큰캐시 공유, 컨트롤플레인/대시보드.
- **피벗 시(별도)**: 멀티테넌트/키보관 — ToS·금융규제 선결 필수.

---

## 9. 미해결 / 확인 필요 (TODO)

- 토스 Open API **ToS** 가 개인 client_credentials 의 원격 자가호스팅(단일테넌트, secret 미공유)을 제약하는지 — 셀프호스트라 위험은 낮으나 확인.
- FastMCP Streamable HTTP 의 정확한 ASGI 마운트/미들웨어 주입 API 표면(구현 시 SDK 버전 확인).
- redis-py `Lock` 의 fakeredis 지원 범위 실측(블로킹/타임아웃/소유권 토큰).
- Docker 템플릿에 Redis AOF 기본 on 명시.

---

## 부록. 영향받는 불변식 (구현 후 CLAUDE.md / docs/claude 갱신 대상)

- `place_order`/`modify_order` 안전 불변식: "성공 시 finalize" → **"시도 시 reserve / 실패 시 release / 성공 시 유지"** (분산안전 정제, 효과 동일).
- 일일누적 복원: redis 백엔드는 카운터가 진실의 원천(감사 리플레이 불필요), memory 백엔드만 `restore_spend` 유지.
- 신규 모드 축: `transport`(stdio|http) + `state_backend`(memory|redis) — 기존 `mode`(read_only|paper|live) 와 직교.
