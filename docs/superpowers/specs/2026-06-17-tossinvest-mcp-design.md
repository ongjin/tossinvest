# 토스증권 MCP + Python SDK — 설계 문서

- **작성일**: 2026-06-17
- **상태**: 설계 확정 (구현 전)
- **한 줄 요약**: 토스증권 Open API 를 (1) decimal·rate-limit·멱등성·에러까지 제대로 다루는 Python 클라이언트 SDK 와 (2) 그 위에 안전 가드레일을 얹어 LLM 에 증권계좌를 쥐여주는 MCP 서버, 두 오픈소스 패키지로 만든다.

---

## 1. 목표와 배경

### 1.1 무엇을, 왜

"AI 만 연결하면 내 증권계좌를 읽고/거래할 수 있는" 도구를 **오픈소스**로 만든다. 가장 자연스러운 형태는 **MCP 서버** — 사용자가 자기 Claude/Cursor/ChatGPT 등 MCP 클라이언트에 붙이면 "내 잔고 보여줘 / 삼성전자 5주 사줘 / 환율 추세 분석해줘" 가 바로 된다.

### 1.2 1순위 목표 — 오픈소스 평판/포트폴리오

수익이 아니라 **평판/포트폴리오가 1순위**. 그래서:

- 크래프트(엔지니어링 품질)가 드러나는 구조를 택한다 — 자동생성 클라이언트 래핑(X), 직접 잘 만든 SDK(O).
- "어떻게 LLM 에게 진짜 증권계좌 키를 안전하게 쥐여주나" 라는 차별점을 전면에 둔다 — 이게 블로그 글의 메인 떡밥이자 신뢰의 근거.
- 수익화는 v1 범위 밖. 순수 OSS 로 내고, README 에 GitHub Sponsors + (나중에) BYOK 호스팅 편의 티어 여지만 남긴다.

### 1.3 핵심 제약 — API 가 사전신청 단계

검증 시점(2026-06-17) 기준 토스증권 Open API 는 **사전신청 단계**(정식 오픈일 미정)다. 정식 `client_id`/`client_secret` 발급 전까지 **라이브 테스트가 불가**.

→ 이 제약이 설계를 한 방향으로 강제한다: **라이브 키 없이도 전부 만들고·테스트하고·데모할 수 있어야 한다.** 그래서 페이퍼(시뮬) 모드 + mock fixture 가 선택이 아니라 필수다. 그리고 이게 주문 기능을 *지금 당장 안전하게* 만들 수 있게 해주는 부수효과까지 준다.

### 1.4 범위 (v1)

- **읽기 + 주문(매수·매도) 둘 다** 처음부터 포함.
- 단, 주문은 기본 페이퍼 모드 + 가드레일 뒤에 둔다(§3).

### 1.5 비목표 (Non-goals, YAGNI)

- 백테스팅 프레임워크, 전략 라이브러리 — 별도 프로젝트/후속.
- 멀티 브로커 추상화 — 토스 전용으로 시작.
- 실시간 스트리밍 — 토스 API 가 WebSocket 미지원(REST/폴링뿐).
- 유료 신호 판매·투자자문 기능 — 규제 지뢰밭(§7), 의도적으로 안 만든다.
- 웹 대시보드/프론트엔드 — v1 은 SDK + MCP 서버까지.

---

## 2. 아키텍처 — 레이어드 (모노레포, 두 패키지)

`tossinvest-mcp` 가 `pytossinvest` 에 의존하는 모노레포. 퀀트 유저는 SDK 만 `pip install` 해서 직접 쓰고, AI 유저는 MCP 서버를 클라이언트에 꽂는다. 경계가 SDK↔MCP 로 깨끗하게 갈린다.

```
pytossinvest/            # SDK — pip install pytossinvest
  auth.py                # 토큰 매니저 (만료 전 갱신, 401 expired-token 1회 재발급)
  ratelimit.py           # 그룹별 토큰버킷 + 429 Retry-After/백오프/jitter, 9:00~9:10 ORDER 반토막
  errors.py              # code → 예외 계층 매핑, unknown-code 관용
  money.py               # 문자열 ↔ Decimal (float 금지)
  models.py              # 응답 타입 (pydantic, decimal-safe)
  client.py              # TossInvestClient — result 언래핑·계좌헤더·accountSeq 캐싱
tossinvest-mcp/          # MCP 서버 — pytossinvest 의존
  server.py              # 모드별 툴 등록
  tools/                 # read / write 툴 정의
  safety.py              # 모드·가드레일·preview/confirm·멱등성
  paper.py               # 페이퍼 트레이딩 엔진
  config.py              # env (pydantic-settings)
tests/
  fixtures/              # openapi.json 스키마 기반 녹화 응답
  (respx mock transport)
```

각 유닛의 책임:

| 유닛 | 한다 | 의존 |
|---|---|---|
| `auth` | 토큰 발급·캐싱·갱신, 401 재시도 | httpx |
| `ratelimit` | 그룹별 송신 속도 제어, 429 대응 | — |
| `errors` | HTTP/code → 예외, unknown 관용 | — |
| `money` | 문자열 decimal 안전 변환 | Decimal |
| `models` | 응답 스키마(decimal-safe) | pydantic |
| `client` | 엔드포인트 메서드, result 언래핑, accountSeq 캐싱 | 위 전부 |
| `safety` | 모드 분기·가드레일·preview/confirm·멱등성 | client |
| `paper` | 시뮬 포트폴리오·체결 | money, models |
| `server`/`tools` | MCP 툴 등록·in/out 스키마 | safety, client, paper |

---

## 3. 안전모델 (프로젝트의 핵심)

### 3.1 모드 3단계 — 기본값이 안전 쪽

`TOSSINVEST_MODE` (default `paper`):

- **`read_only`** — 주문 툴을 **아예 등록 안 함**. LLM 이 매매 자체가 불가능.
- **`paper`** *(기본값)* — 주문 툴은 있되 **로컬 시뮬 포트폴리오**(`paper.py`)에 체결. 실주문 0 건. 라이브 키 없이도 전체 주문 흐름을 만들고 데모 가능.
- **`live`** — 실주문. 모드만으론 부족하고 `TOSSINVEST_ALLOW_LIVE=1` 까지 있어야 켜짐(이중 안전장치).

### 3.2 가드레일 (live 필수, paper 옵션)

- 주문당 금액 상한 / 일일 누적 상한 (config, 기본 보수적).
- 종목 allow/deny 리스트(옵션).
- `/market-calendar` 로 장운영 시간 게이트(휴장·장외 거부, override 가능).
- **1억원↑ 주문**: `confirmHighValueOrder` 를 **자동 설정하지 않음** — 사용자가 명시 안 하면 거부(LLM 이 멋대로 거액 못 지름).
- **30억원↑**: 클라이언트단에서 즉시 거부(API 도 `422` 거부하지만 왕복 전에 fail-fast).

### 3.3 2단계 주문 (human-in-the-loop)

- **`preview_order`** — tick size·매수가능금액(`/buying-power`)·매도가능수량(`/sellable-quantity`)·가드레일·장운영 시간을 검증하고 **예상 비용/수수료/체결후 잔고** 를 리턴, **실행 안 함**. 짧게 유효한 `confirmation_token`(파라미터에 바인딩) 발급.
- **`place_order`** — 그 `confirmation_token` 없으면 실행 거부. LLM 이 한 방에 YOLO 매매 못 하게 강제. `clientOrderId` 멱등성 자동 관리(재시도 중복주문 방지).
- 바깥 방어선: MCP 클라이언트 자체 툴 승인 UX(Claude Desktop 등 사람이 각 호출 승인)를 문서화. 단 이게 유일한 방어선이 아님 — 서버단 가드레일이 1차.

### 3.4 멱등성

주문 생성 시 `clientOrderId` 자동 부여·추적(10분 윈도우). 전송 실패 재시도는 **같은 키** 재사용 → 중복주문 방지(API 공식 권장).

### 3.5 감사 로그

모든 write 툴 호출(파라미터·판단·결과·`requestId`)을 로컬에 기록. 신뢰·디버깅·블로그 스크린샷용.

---

## 4. MCP 툴 목록 (엔드포인트 → 툴 매핑)

15 개 엔드포인트를 ~12 개 툴로 의도적으로 통합해 LLM 컨텍스트를 절약한다. 툴 description 에 "돈/수량은 문자열", "preview 먼저" 같은 제약을 박아둔다.

**읽기 (항상 등록):**

| 툴 | 엔드포인트 | 비고 |
|---|---|---|
| `get_accounts` | `/accounts` | accountSeq 캐싱(ACCOUNT 1/s) |
| `get_holdings` | `/holdings` | 잔고·수익률, `amountAfterCost` 그대로 |
| `get_quote` | `/prices`·`/orderbook`·`/trades` | 다건 최대 200 묶기 |
| `get_candles` | `/candles` | 1m/1d, 과거 페이징 |
| `get_stock_info` | `/stocks`·`/warnings` | 종목정보 + 매수유의 |
| `get_market_info` | `/exchange-rate`·`/market-calendar` | 환율·장운영 |
| `list_orders` / `get_order` | `/orders`·`/orders/{id}` | OPEN 만(CLOSED 미지원 명시) |

**거래 사전조회 (write 보조):**

- `get_order_readiness` — `/buying-power` + `/sellable-quantity` + `/commissions` 묶음. `preview_order` 가 내부적으로도 사용.

**쓰기 (모드·가드레일 게이트):**

- `preview_order` → `place_order` (2단계, §3.3)
- `modify_order` · `cancel_order` — **새 orderId 반환** → 원주문 매핑 유지. US 정정은 가격만(quantity 거부, `us-modify-quantity-not-supported`).

---

## 5. 크로스커팅 인프라 (SDK 에 집중 = 엔지니어링 가치)

- **토큰 매니저**: `expires_in` 만료 전 갱신 + 메모리 캐싱, `401 expired-token` 시 1회 재발급 후 재시도. **토큰 엔드포인트만 응답 포맷이 다름**(OAuth2 표준, `result` 래핑 안 함; 실패는 `{error, error_description}`) → 분기.
- **레이트리미터**: 그룹별(AUTH/ACCOUNT/MARKET_DATA/ORDER…) 토큰버킷을 클라이언트단에 구현. **헤더가 진실**(`X-RateLimit-*`) — 표 숫자 하드코딩 금지, 헤더로 동적 보정. 429 → `Retry-After` + 지수 백오프 + jitter. **9:00~9:10 KST ORDER/ORDER_INFO 반토막(6→3)** 반영.
- **Decimal 안전**: 금액·수량은 문자열 그대로 받아 `Decimal`. 직렬화 시 다시 문자열. **float 변환 경로 자체를 안 만든다.**
- **에러 매핑**: `code`(flat string) → 예외 계층(`AuthError`/`RateLimitError`/`OrderRejected`/…). `message` 가 빈 값일 수 있으니 **code 기준 분기**. **unknown code/enum 관용** — 모르는 값이면 base 예외로 떨어지되 안 깨짐(API 가 값 추가 가능하다고 명시).
- **응답 언래핑**: `result` 자동 언래핑(토큰 엔드포인트 제외). 목록형은 배열.
- **계좌 컨텍스트**: 계좌·자산·주문 API 는 `X-Tossinvest-Account` 헤더 필요. `accountSeq` 는 부팅 시 1회 받아 캐싱(ACCOUNT 1/s).
- **설정**: env (`TOSSINVEST_CLIENT_ID/SECRET`, `TOSSINVEST_MODE`, `TOSSINVEST_ALLOW_LIVE`, 금액 상한 등). pydantic-settings.

---

## 6. 테스트 전략 (라이브 키 없이 CI 통과·기여 가능)

API 가 사전신청 단계라 **라이브 의존 없이 전부 테스트 가능**하게 만드는 게 핵심.

- **Mock transport (respx)** + `openapi.json` 스키마에서 뽑은 **fixture 응답**으로 SDK 단위 테스트. 실 네트워크 0.
- **유닛 타깃**: 레이트리미터(토큰버킷·429·피크반토막), Decimal 왕복, 에러코드 매핑(unknown 포함), `result` 언래핑, 멱등성 키 재사용, 토큰 갱신/401 재시도.
- **페이퍼 엔진 테스트**: 매수→잔고 반영→매도→실현손익 시뮬 일관성, preview→confirm 토큰 바인딩, 가드레일(상한 초과 거부·장외 거부).
- **MCP 통합 테스트**: 모드별 툴 등록 여부(read_only 면 write 툴 부재), 툴 in/out 스키마.
- **계약 테스트(옵션, `@live` 마크)**: 정식 키 생기면 소수 테스트로 fixture 가 실제와 안 어긋났는지 검증.

→ 결과: 누구나 `git clone && uv sync && pytest` 로 그린. 기여 장벽이 낮고, README 에 "라이브 계좌 없이 페이퍼로 데모" 가능.

---

## 7. 규제·법적 메모 (수익모델을 결정함)

- 한국에선 **남의 계좌로 대신 매매 결정** = 투자일임업, **돈 받고 구체적 매매신호 제공** = 유사투자자문 신고 대상.
- **하지만 "사용자가 자기 키로 자기 계좌에서 직접 돌리는 도구"는 규제 대상이 아니다.** 이게 오픈소스 self-host 도구가 깔끔한 이유.
- 따라서 우리는 **도구만 제공하고 매매 결정·신호는 제공하지 않는다.** 수익화는 ① GitHub Sponsors ② (후속) BYOK 호스팅 편의 티어 ③ 콘텐츠(블로그→강의/광고) 로 한정. "신호 팔기" 는 의도적으로 안 한다.

---

## 8. 미해결/확인 필요 (TODO)

- **네이밍**: `pytossinvest` / `tossinvest-mcp` 는 잠정. **"비공식(unofficial) 클라이언트"임을 명시**(토스 상표/엔도르스먼트 오해 방지)하고 PyPI 이름 가용성·상표 마찰을 출시 전 확인.
- **정식 API 키**: 사전신청 → 발급 후에야 `@live` 계약 테스트 가능. 그 전까지 fixture 가 스펙과 어긋날 리스크 → openapi.json 을 source of truth 로 주기적 재확인.
- **패키징 도구**: uv 워크스페이스(hatch 빌드) 가정. 확정 필요.

---

## 9. 라이선스 (듀얼)

모노레포지만 패키지별로 라이선스를 나눈다:

- **`pytossinvest` (SDK)** — **MIT**. 채택 마찰 최소화. SDK 는 어디든 끼워 쓸 수 있게.
- **`tossinvest-mcp` (MCP 서버)** — **Apache 2.0**. 특허 보호조항(grant/retaliation) 포함.

루트에 `LICENSE`(MIT)·`LICENSE-APACHE` 를 두고, 각 패키지 디렉터리에 해당 라이선스 파일과 `pyproject.toml` 의 `license` 필드를 명시. README 에 "SDK=MIT, MCP=Apache-2.0" 를 분명히 표기.

---

## 부록 A. 의존 레퍼런스

토스증권 Open API 상세 스펙(인증 2층·엔드포인트·rate limit·에러코드·주문 스키마·함정)은 별도 레퍼런스 문서 참조:
`~/workspace/personal/blog/docs/claude/tossinvest-open-api.md` (canonical: `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`).
