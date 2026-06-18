# pytossinvest-mcp

**LLM(Claude Desktop·Cursor 등)에게 토스증권 계좌를 *안전하게* 쥐여주는 비공식 MCP 서버.** [`pytossinvest`](../pytossinvest/) SDK 위에, **"AI 가 멋대로 내 계좌를 질러버리면?"** 을 클라이언트 신뢰가 아니라 **서버단 가드레일**로 막는 안전모델을 얹었습니다.

![python](https://img.shields.io/badge/python-3.12+-3776ab)
![license](https://img.shields.io/badge/license-Apache--2.0-d22128)
![tests](https://img.shields.io/badge/tests-166%20passing-2ea44f)
![status](https://img.shields.io/badge/Toss%20API-pre--launch-f0ad4e)
![unofficial](https://img.shields.io/badge/unofficial-%E2%9A%A0-9e9e9e)

> ⚠️ **비공식 MCP 서버** — 토스증권과 무관하며 상표/엔도르스먼트와도 무관합니다. **transport 는 기본 stdio**(Claude Desktop·Cursor 등 로컬 MCP 클라이언트용) — 원격 **http** 도 선택 가능(아래 [HTTP 원격 모드](#http-원격-모드-docker--redis)).
>
> 토스 Open API 는 2026-06 기준 **사전신청 단계**. **기본 모드 `paper`** 는 주문을 로컬 시뮬로 체결(실주문 0)하되 **시세는 실제로 읽으므로 API 키가 필요**합니다. 테스트 스위트는 완전 오프라인.

---

## 🔒 핵심 — 안전모델

이게 이 패키지의 존재 이유입니다. LLM 이 한 방에 YOLO 매매를 못 하도록 **3중 방어**:

### 1. 모드 게이트 — 기본값이 안전 (fail-closed)

| 모드 | 주문 | 동작 | 켜는 법 |
|---|:---:|---|---|
| `read_only` | ✗ | 읽기만, **주문 툴 아예 미등록** | `TOSSINVEST_MODE=read_only` |
| **`paper`** *(기본)* | ○ | 로컬 시뮬 포트폴리오 체결, **실주문 0** | (기본값) |
| `live` | ○ | 실주문 | `MODE=live` **+** `ALLOW_LIVE=1` |

`live` 는 `TOSSINVEST_MODE=live` **와** `TOSSINVEST_ALLOW_LIVE=1` 이 *둘 다* 있어야 켜집니다(이중 게이트). 모드만 바꿔선 아무 일도 안 일어납니다 — `allow_live` 없이 `mode=live` 면 서버가 시작 시 `ValueError` 로 거부.

### 2. 2단계 주문 — human-in-the-loop

`preview_order` 가 가드레일을 검사하고 예상 비용과 함께 **짧게 유효한 confirmation token**(기본 120초)을 발급. `place_order` 는 그 토큰 없이는 **거부**합니다. LLM 이 미리보기 없이 한 번에 체결할 수 없습니다. **정정(modify)도 같은 2단계** — `preview_modify`→`modify_order(confirmation_token)` 로, 정정 *후* notional 에 가드레일을 적용합니다(소액 주문을 낸 뒤 거액으로 정정하는 우회 차단).

> ⚠️ **live + 자동승인(auto-approve) 클라이언트 금지** — 2단계는 *사람이 각 호출을 승인*한다는 전제입니다. MCP 클라이언트가 툴 호출을 자동 승인하면 LLM 이 한 턴에 preview→place(또는 preview_modify→modify)를 연달아 호출해 사람이 못 낍니다. live 에선 각 툴 승인을 **수동**으로 두고, 추가 방어로 `LIVE_CONFIRM_MIN_DELAY_SEC`(아래 설정)로 미리보기→실행 사이 최소 간격을 강제하세요.

### 3. 가드레일 — paper·live 모두 적용

주문당/일일 누적 금액 상한(**통화별** — KRW/USD 분리) · 종목 allow/deny · **고액 확인 필수**(KRW 1억 / USD $100k↑) · **하드 실링 즉시 거부**(KRW 30억 / USD $3M↑) · 장운영시간(live 전용).

> **불변식:** 체결·정정 경로(`paper.place` / `client.place_order` / `client.modify_order`)는 **반드시** `check_guardrails` 를 통과합니다. 토큰은 `preview_order`·`preview_modify` 에서 가드레일 통과 *후에만* 발급됩니다. 우회 경로가 없습니다.

---

## 설치 & 실행

```bash
uv sync --package pytossinvest-mcp --extra dev
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tossinvest": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/toss",
               "--package", "pytossinvest-mcp", "pytossinvest-mcp"],
      "env": {
        "TOSSINVEST_MODE": "paper",
        "TOSSINVEST_CLIENT_ID": "...",
        "TOSSINVEST_CLIENT_SECRET": "..."
      }
    }
  }
}
```

직접 실행:

```bash
TOSSINVEST_MODE=paper TOSSINVEST_CLIENT_ID=... TOSSINVEST_CLIENT_SECRET=... \
  uv run --package pytossinvest-mcp pytossinvest-mcp
```

기본 `paper` 라 실주문은 0건 — 안심하고 *"삼성전자 10주 미리보기 해줘"* 부터 시켜보세요. 실거래로 가려면 `TOSSINVEST_MODE=live` **+** `TOSSINVEST_ALLOW_LIVE=1`.

---

## HTTP 원격 모드 (Docker + Redis)

로컬 stdio 대신 **Streamable-HTTP** 로 띄울 수 있습니다. `deploy/` 아래 Docker Compose 스택을 제공합니다 — 앱 인스턴스 1개 + Redis(AOF 활성화).

> **기본값은 stdio 그대로.** `TOSSINVEST_TRANSPORT` 를 설정하지 않으면 종전과 동일하게 stdio 로 동작합니다 — 기존 Claude Desktop 사용자는 아무 것도 바꿀 필요 없습니다.

### 빠른 시작

```bash
# remote http mode (single command, app + Redis)
cd deploy
cp .env.example .env   # then edit .env (set TOSSINVEST_AUTH_TOKEN, keys)
docker compose up --build
# → MCP Streamable-HTTP at http://localhost:8000/mcp  (Authorization: Bearer <token>)
```

### 필수 사항

- **`TOSSINVEST_AUTH_TOKEN` 필수** — 이 값 없이는 http 모드로 서버가 부팅을 거부합니다. `.env.example` 을 `.env` 로 복사하고 충분히 긴 랜덤 값으로 설정하세요. **실제 `.env` 는 절대 커밋하지 마세요.**
- **MCP 엔드포인트**: `http://<host>:8000/mcp`
- **인증**: 모든 MCP 클라이언트 요청에 `Authorization: Bearer <token>` 헤더 필요. 헤더가 없거나 토큰이 틀리면 `401` 으로 거부합니다.

### 설정 변수 (HTTP 관련)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `TRANSPORT` | `stdio` | `stdio` · `http` |
| `HTTP_HOST` | `127.0.0.1` | 바인드 주소 (컨테이너 내부에선 `0.0.0.0`) |
| `HTTP_PORT` | `8000` | 리슨 포트 |
| `AUTH_TOKEN` | — | Bearer 토큰. **http 모드에서 필수** |
| `STATE_BACKEND` | `memory` | `memory` · `redis` (http 모드에선 `redis` 권장) |
| `REDIS_URL` | — | `redis://...` (STATE_BACKEND=redis 시 필요) |
| `HTTP_ALLOWED_HOSTS` | `[]` | JSON 리스트. 선택적 Host 핀닝(심화 방어). 비면 DNS-rebinding 보호 off(인증은 bearer). 예 `["mcp.example.com","mcp.example.com:*"]` |

> **Host 핀닝(`HTTP_ALLOWED_HOSTS`)은 선택입니다.** 기본은 DNS-rebinding 보호를 끄고 **bearer 토큰을 유일한 인증면**으로 둡니다(배포 호스트는 보통 리버스 프록시 뒤라 빌드 시점에 알 수 없음). 운영자가 자기 호스트를 알면 JSON 리스트로 핀닝해 심화 방어를 켤 수 있습니다 — 그 경우 목록에 없는 `Host` 헤더는 `421` 로 거부됩니다(`:*` 로 포트 와일드카드).

> **원격 자가호스팅 & 약관.** 이 스택은 **단일 테넌트** 전용입니다 — 본인 토스 자격증명(`CLIENT_ID`/`CLIENT_SECRET`)으로 본인 인프라에서만 돌리세요. `AUTH_TOKEN` 은 **MCP 엔드포인트 인증일 뿐**, 토스 자격증명이 아니며 Redis 에도 저장되지 않습니다(자격증명은 서버 env 로만 보관 — 브라우저/클라이언트 노출 금지). 토스 [오픈 API 서비스 이용 약관](https://home.tossinvest.com/ko/terms/v2?id=752)이 client_credentials 자격증명의 제3자 제공·공유를 제한하므로, 토큰을 남에게 넘기거나 다중 사용자에게 공유하지 마세요. 원격 노출에 따른 책임은 운영자에게 있으니 배포 전 최신 약관을 직접 확인하시기 바랍니다.

---

## 설정 (env, prefix `TOSSINVEST_`)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `MODE` | `paper` | `read_only` · `paper` · `live` |
| `ALLOW_LIVE` | `0` | `live` 시작에 필수(이중 게이트). `1` 이어야 함 |
| `CLIENT_ID` / `CLIENT_SECRET` | — | 토스 Open API 자격증명 |
| `BASE_URL` | `https://openapi.tossinvest.com` | API 엔드포인트 |
| `MAX_ORDER_AMOUNT` | `1000000` | 주문당 상한 (**KRW 심볼**, 숫자 코드) |
| `DAILY_ORDER_LIMIT` | `5000000` | 일일 누적 상한 (**KRW 심볼**) |
| `MAX_ORDER_AMOUNT_USD` | `1000` | 주문당 상한 (**USD 심볼**, 영문 티커) |
| `DAILY_ORDER_LIMIT_USD` | `5000` | 일일 누적 상한 (**USD 심볼**) |
| `ALLOW_SYMBOLS` | `[]` | JSON 리스트. 비면 전체 허용. 예 `["005930"]` |
| `DENY_SYMBOLS` | `[]` | JSON 리스트. allow 보다 먼저 검사 |
| `ENFORCE_MARKET_HOURS` | `1` | **live 전용** 장운영시간 게이트 |
| `LIVE_CONFIRM_MIN_DELAY_SEC` | `0` | **live 전용** preview→실행 최소 간격(초). `0`=off. 자동승인 클라이언트 방어로 `5` 권장 |
| `PAPER_STARTING_CASH` | `10000000` | paper 포트폴리오 시작 현금 |
| `CONFIRMATION_TTL_SEC` | `120` | preview→place 토큰 유효시간(초) |
| `AUDIT_LOG_PATH` | `pytossinvest-mcp-audit.log` | 감사 로그(JSONL) 경로 |

> 돈 관련 필드(`MAX_ORDER_AMOUNT`·`DAILY_ORDER_LIMIT`·`MAX_ORDER_AMOUNT_USD`·`DAILY_ORDER_LIMIT_USD`·`PAPER_STARTING_CASH`)는 float 으로 주면 `TypeError` — 문자열/정수만(JSON/Decimal 안전).
>
> **통화 판정**: 심볼이 영문자면 USD(예 `AAPL`), 숫자 코드면 KRW(예 `005930`) 한도·임계로 비교합니다(FX 환산 없음 — 안전 한도가 환율/네트워크에 의존하지 않게).

---

## 14개 툴

입출력의 돈·수량은 **전부 문자열**(JSON/Decimal 안전). 툴 설명(description)에도 string-money / 2단계 주문 / live-only 제약이 명시돼 있어 LLM 이 올바르게 호출합니다.

### 읽기 (모든 모드에서 항상)

| 툴 | 시그니처 | 설명 |
|---|---|---|
| `get_accounts` | `()` | 계좌 목록. paper 면 합성 `PAPER` 계좌 반환 |
| `get_holdings` | `(symbol=None)` | 보유 포지션. paper 면 현금·실현손익·종목 |
| `get_quote` | `(symbols: list)` | 최신가(최대 200종목). **단일 종목이면 호가+체결도 동봉** |
| `get_candles` | `(symbol, interval, count=100, before=None)` | OHLC 캔들. `interval` 은 `'1m'` 또는 `'1d'` |
| `get_stock_info` | `(symbols: list)` | 종목 기본정보(최대 200) |
| `get_market_info` | `(country='KR', base_currency=None, quote_currency=None)` | 시장 캘린더. 통화쌍 주면 환율 동봉 |
| `list_orders` | `(status='OPEN', symbol=None)` | 미체결 주문(실 API 는 OPEN 만). paper 는 시뮬 주문 |
| `get_order` | `(order_id)` | 주문 상세 |

> **시세 툴(`get_quote`·`get_candles`·`get_stock_info`·`get_market_info`)은 모드와 무관하게 항상 실제 client 를 씁니다** — 시세는 계좌와 무관하니까요. 계좌 읽기(`get_accounts`·`get_holdings`·`list_orders`·`get_order`)만 paper 모드에서 시뮬로 라우팅됩니다.

### 쓰기 (`read_only` 외 = paper · live)

| 툴 | 시그니처 | 설명 |
|---|---|---|
| `get_order_readiness` | `(symbol, side='BUY', currency='KRW')` | 주문 전 매수여력·매도가능수량·수수료 |
| `preview_order` | `(symbol, side, order_type, quantity=None, price=None, order_amount=None, time_in_force='DAY', confirm_high_value_order=False)` | **STEP 1/2.** 가드레일 검사 + 비용 추정 → `confirmationToken`. 주문 안 함 |
| `place_order` | `(confirmation_token)` | **STEP 2/2.** 토큰으로 체결. consume 직후·실행 전 일일 한도 재검사. 멱등(실패하면 같은 토큰 재시도 가능) |
| `preview_modify` | `(order_id, order_type, price=None, quantity=None, confirm_high_value_order=False)` | **정정 STEP 1/2** (**live 전용**). 원주문 조회 → 정정 후 notional 로 가드레일 검사 → `confirmationToken`. 정정 안 함 |
| `modify_order` | `(confirmation_token)` | **정정 STEP 2/2** (**live 전용**). 토큰으로 정정 실행, 새 orderId 반환. 멱등 |
| `cancel_order` | `(order_id)` | 취소 (**live 전용**, 새 orderId 반환). 취소 전 원주문 상태를 감사 로그에 기록 |

---

## 2단계 주문 흐름

```
preview_order(...) ──guardrails 통과──▶ confirmationToken (TTL 120s)
                                            │
place_order(confirmation_token) ────────────┘
   │  consume(token)   ← 존재·만료 검증 (pop 안 함)
   │  실행 (paper 체결 / 실주문)
   │  성공 시에만 finalize(token)  ← pop + 일일누적 기록
   └─ 실패 시 finalize 안 함  ← 토큰 살아있음 → 같은 clientOrderId 로 멱등 재시도
```

**멱등성의 핵심**: 토큰은 **성공했을 때만** 소비됩니다. 그래서 `place_order` 가 도중에 실패하면 토큰이 살아있어 **같은 `clientOrderId` 로 안전하게 재시도** — 두 번 체결되지 않습니다. 성공하면 토큰이 사라져 2차 발사도 불가능합니다.

> **일일 한도 재검사**: 일일 누적(`_spent`)은 place **성공 시점**에만 증가합니다. 그래서 `place_order` 는 `consume` 직후·실행 전에 금액 가드레일(주문당·일일)을 **다시** 검사합니다 — preview 를 여러 번 받아 모았다가 한꺼번에 place 해 한도를 넘기는 우회를 막습니다.
>
> **정정(modify)도 같은 토큰 게이트**: `preview_modify` 가 원주문을 조회해 정정 후 주문을 만들고 가드레일을 통과한 뒤 토큰을 발급, `modify_order(confirmation_token)` 가 실행합니다. 정정은 *기존 주문 금액의 변경*이라 일일 누적엔 증분(`new−old`)만 가산하고, 주문당·고액·하드실링 게이트는 정정 후 notional 전액에 적용됩니다.

> notional(주문금액) 계산 우선순위: `order_amount` → `price × quantity` → `ref_price × quantity`(MARKET 추정가). 셋 다 불가능하면 `insufficient-order-params`, `order_amount` 를 `price`/`quantity` 와 **함께** 주면 `invalid-order-params` 로 거부합니다. `quantity`·`price`·`order_amount` 가 **0 이하면** `invalid-order-value`(음수 notional 이 상한을 통과하던 구멍 차단).

---

## 가드레일 (검사 순서)

`build_spec` 이 먼저 **비양수 값**(`quantity`·`price`·`order_amount` ≤ 0)을 `invalid-order-value` 로 거부합니다. 그다음 `check_guardrails` 가 아래 **순서대로** 검사합니다(순서가 동작의 일부 — 상한은 크게 두고 고액/한도가 먼저 터지게):

1. **deny 심볼** → `symbol-denied`
2. **allow 심볼**(allow 리스트가 있는데 없으면) → `symbol-not-allowed`
3. **하드 실링 초과** → `max-order-exceeded` *(무조건 거부 — KRW `> 30억` / USD `> $3M`)*
4. **고액 + 미확인** → `confirm-high-value-required` *(KRW `>= 1억` / USD `>= $100k`, `confirm_high_value_order=true` 필요)*
5. **주문당 상한 초과** → `order-amount-cap` *(통화별: `MAX_ORDER_AMOUNT` / `MAX_ORDER_AMOUNT_USD`)*
6. **일일 누적 상한 초과** → `daily-limit` *(통화별 버킷: `DAILY_ORDER_LIMIT` / `DAILY_ORDER_LIMIT_USD`)*
7. **장 마감**(live + `enforce_market_hours` 일 때만) → `market-closed`

> **심볼 정규화**: deny/allow 비교 전 심볼을 **NFKC 정규화 + 공백·제어문자 제거 + 대문자화**합니다 — `Ａ Ａ Ｐ Ｌ` 같은 전각/구분자 트릭으로 deny 리스트를 빠져나가지 못하게.

> notional 은 **주문 통화별**로 비교합니다 — 심볼이 영문이면 USD, 숫자면 KRW 의 상한·임계·일일 버킷을 씁니다(FX 환산 안 함). 일일 누적은 KST 날짜로 리셋되고, **서버 재시작 시 오늘자 감사 로그(JSONL)를 replay 해 통화별로 복원**됩니다(재시작으로 한도가 0 으로 풀리는 구멍 차단).

---

## 동작 메모 (이미 겪었거나 설계로 막은 것)

- **paper modify/cancel 은 live 전용** — paper 는 즉시체결 모델이라 정정/취소할 미체결 주문이 없습니다. paper 에선 `preview_modify`·`cancel_order` 가 `PaperError` 로 명확히 거부(실제 `409 already-filled` 미러링).
- **paper MARKET 무가격 체결 금지** — 체결 시점에 참조가(ref price)가 비면 가격 0 으로 조용히 체결되던 버그를 막았습니다. ref price 없으면 `PaperError`(토큰 살려둠 → 재시도 가능). US 금액주문은 `qty = order_amount / fill_price`(Decimal 나눗셈).
- **장운영시간 US 자정넘김** — 미국장을 KST 로 표기하면 23:30→06:00 처럼 자정을 넘깁니다. `start > end` 면 wrap 윈도우(`now >= start or now < end`)로 처리. 깨진 시간 문자열은 "닫힘"으로 안전 처리. 종목 코드가 영문자면 `US`, 아니면 `KR` 로 캘린더 조회.
- **감사 로그** — 모든 write 결정(`previewed`/`placed`/`modify_previewed`/`modified`/`canceled`/`error`)을 `AUDIT_LOG_PATH` 에 **JSONL(append-only)** 로 기록합니다(UTC 타임스탬프). 신뢰·디버그·기록 용도. `placed`·`modified` 레코드엔 `notional`·`currency` 가 들어가 **재시작 시 일일 누적 복원(replay)** 에 쓰이고(`modified` 는 delta 기록), `modify_previewed`·`canceled` 엔 **정정/취소 전 원주문 상태**가 남습니다.

---

## 테스트

```bash
uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests   # 166 passing
```

`FakeClient` + paper 엔진으로 검증 — **라이브 키 불필요, 네트워크 0**. 무거운 로직(가드레일·토큰·paper·market_hours·audit)은 pure 모듈로 분리해 직접 단위테스트하고, `server.py` 는 모드별 **툴 등록 여부**만 검증합니다(MCP 트랜스포트 내부에 의존 안 함).

---

## 라이선스

**Apache-2.0** — 패키지 디렉터리의 [`LICENSE`](LICENSE) · [`NOTICE`](NOTICE) 참고. (의존하는 SDK [`pytossinvest`](../pytossinvest/) 는 MIT.)
