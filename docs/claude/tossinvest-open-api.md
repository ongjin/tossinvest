> **언제 읽나**: 이 레포(`pytossinvest` SDK + `pytossinvest-mcp` MCP 서버)가 다루는 **토스증권 Open API 의 코어 레퍼런스**. 인증 2단 구조·엔드포인트·요청/응답 스키마·rate limit·에러코드·도메인 함정의 단일 소스. **SDK 엔드포인트 추가·MCP 툴 작업·주문 로직 손대기 전 여기부터 읽는다.** (원래 블로그(zerry.co.kr) `docs/claude/` 에 있다가 이 레포로 이전됨 — 블로그 글감: `tossinvest-open-api-guide`.)
>
> **🔄 자가갱신**: 이 문서는 **스냅샷**이다. canonical `openapi.json` 과 어긋나거나, 코드 작업 중 새 필드·엔드포인트·enum·함정을 발견하면 **그 세션에서 바로 이 문서를 갱신**한다(커밋은 수동). 권위 순서는 ① openapi.json → ② developers.tossinvest.com/docs → ③ 본 문서.

# 토스증권 Open API 레퍼런스

- **출처(canonical)**: `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json` (OpenAPI **3.1.0**, `info.version` **1.1.1**)
- **문서 허브**: https://developers.tossinvest.com/docs · AI/평문용 `https://developers.tossinvest.com/llms.txt` · 개요 `…/openapi-docs/overview.md`
- **Base URL**: `https://openapi.tossinvest.com` (모든 경로 prefix)
- **연동**: REST only. **WebSocket/실시간 스트리밍 없음** → 실시간이 필요하면 폴링(+ rate limit 고려).
- **검증 시점**: 2026-06-19(canonical 재확인). ⚠ 한도·정책·엔드포인트는 사전 공지 없이 바뀔 수 있다.
- **이 문서는 스냅샷이다 — 막히거나 의심되면 반드시 공식 문서를 본다**: 권위 순서는 ① openapi.json(canonical 스펙) → ② [developers.tossinvest.com/docs](https://developers.tossinvest.com/docs)(인터랙티브) → ③ 본 문서. 이 문서에 없는 필드·새 엔드포인트·세부 enum·정확한 한도 수치는 위 openapi.json 을 source of truth 로 삼아 재확인. (LLM/스크립트면 `…/openapi-docs/overview.md` + `…/api-reference/README.md` 가 평문이라 파싱 쉽다.)

## 0. 자격 / 키 발급 (시점 한정 정보)

- 대상: **토스증권 계좌 보유자**. canonical overview 는 사전신청/대기자 단계 없이 **WTS 에서 직접 키 발급**만 안내(아래) — 2026-06-17 스냅샷의 "약관동의→본인인증→사전신청→순차 오픈 알림" 게이트는 더는 문서에 없음(2026-06-19 재확인).
- **토스증권 WTS(PC 웹)** 로그인 → `설정 > Open API` 메뉴에서 `client_id` / `client_secret` 발급(셀프서비스).
- 국내주식 수수료 2026-06 까지 면제 프로모션 안내가 있었음(시점 한정, 신청 시 재확인).
- 현재 `accounts` 는 **종합매매(BROKERAGE) 계좌만** 반환. 자녀계좌 사용 불가.

---

## 1. 인증 — 두 겹이다

모든 호출에 OAuth 2.0 토큰이 필요하고, **계좌 컨텍스트가 필요한 API(계좌·자산·주문)** 는 토큰에 더해 계좌 헤더까지 필요하다.

### 1층 — OAuth 2.0 Client Credentials

```bash
curl -X POST 'https://openapi.tossinvest.com/oauth2/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=client_credentials' \
  -d 'client_id=xxx' \
  -d 'client_secret=yyy'
```

- 요청 바디는 **`application/x-www-form-urlencoded`** (JSON 아님). `grant_type=client_credentials` 고정.
- 성공 응답(`OAuth2TokenResponse`, **OAuth2 표준 포맷** — 아래 공통 envelope 안 씀):
  ```json
  { "access_token": "eyJhbGciOi...", "token_type": "Bearer", "expires_in": 3600 }
  ```
  - `access_token`: JWT. `expires_in`: 만료까지 남은 **초**(값은 발급해봐야 확정).
- 이후 **모든** 요청에 `Authorization: Bearer {access_token}`.
- 실패 응답도 OAuth2 표준(`OAuth2ErrorResponse`): `{ "error": "invalid_client", "error_description": "..." }` — `error` 값 enum: `invalid_request` · `invalid_client` · `invalid_grant` · `unauthorized_client` · `unsupported_grant_type`. (BFF 공통 에러 envelope 과 다르니 토큰 엔드포인트만 분기 처리.)

### 2층 — 계좌 헤더 `X-Tossinvest-Account`

- 계좌·자산·주문 API 는 `X-Tossinvest-Account: {accountSeq}` 를 추가로 보낸다.
- `accountSeq` 는 `GET /api/v1/accounts` 응답의 `accountSeq`(integer) 에서 얻는다. **한 번 받아 캐싱**(ACCOUNT 그룹이 초당 1회라 매번 부르면 막힌다).
- 누락 시 `400 account-header-required`, 잘못된 계좌면 `404 account-not-found`.

```bash
curl 'https://openapi.tossinvest.com/api/v1/holdings' \
  -H 'Authorization: Bearer {token}' \
  -H 'X-Tossinvest-Account: 1'
```

---

## 2. 공통 규약 (전 엔드포인트 공통 — 반드시 숙지)

- **성공 envelope**: 토큰 발급을 제외한 모든 200 응답은 `ApiResponse` 로 감싼다 → 실제 payload 는 **`result`** 안에 있다.
  ```json
  { "result": { ...엔드포인트별 타입... } }
  ```
  목록형은 `result` 가 배열(예: prices → `result: PriceResponse[]`).
- **에러 envelope**: 4xx/5xx 는 `ErrorResponse`:
  ```json
  { "error": { "requestId": "01HXY…", "code": "invalid-request", "message": "…", "data": { "field": "side", "allowedValues": ["BUY","SELL"] } } }
  ```
  - `code` 가 진짜 식별자(flat string). `message` 는 빈 문자열일 수 있으니 **`code` 기준으로 분기**. `data` 는 코드별로 키가 다르고 없으면 생략.
  - **unknown code/enum 을 허용하도록** 구현하라고 문서가 명시(서버가 값 추가 가능).
- **돈/수량은 전부 문자열 decimal** (`"price": "70000"`). 부동소수점 반올림 방지 — 파싱 시 Decimal 라이브러리 권장, JSON 에 따옴표 필수.
- **시간은 ISO 8601, KST(+09:00) 기준** 표기(필드 설명에 명시). 날짜 파라미터는 `YYYY-MM-DD`.
- **추적 ID**: 응답 헤더 `X-Request-Id` = body 의 `requestId`. CS 문의 시 첨부. 누락 시 헤더 `cf-ray` 첨부(앞단이 Cloudflare).
- **페이지네이션**: cursor 방식. `GET /orders` 만 해당 — 응답 `nextCursor`/`hasNext`, 다음 호출에 `cursor` 전달. 캔들은 별도로 `nextBefore`(→ `before` 파라미터)로 과거 페이징.
- **심볼**: KR = 6자리 숫자(삼성전자 `005930`), US = 영문 티커(`AAPL`). 다건 조회(prices·stocks)는 콤마 구분 **최대 200개**.

---

## 3. Rate Limits

**클라이언트 × API 그룹** 단위 초당 요청 수(TPS) 제한. 각 엔드포인트 description 끝에 소속 그룹이 적혀 있다.

| 그룹 | 한도 | 피크(09:00~09:10 KST) | 소속 엔드포인트 |
|---|---|---|---|
| `AUTH` | 5/s | — | `POST /oauth2/token` |
| `ACCOUNT` | **1/s** | — | `GET /accounts` |
| `ASSET` | 5/s | — | `GET /holdings` |
| `STOCK` | 5/s | — | `GET /stocks`, `…/warnings` |
| `MARKET_INFO` | 3/s | — | `GET /exchange-rate`, `/market-calendar/*` |
| `MARKET_DATA` | 10/s | — | `GET /orderbook`, `/prices`, `/trades`, `/price-limits` |
| `MARKET_DATA_CHART` | 5/s | — | `GET /candles` |
| `ORDER` | 6/s | **3/s** | `POST /orders`, `…/modify`, `…/cancel` |
| `ORDER_HISTORY` | 5/s | — | `GET /orders`, `/orders/{orderId}` |
| `ORDER_INFO` | 6/s | **3/s** | `GET /buying-power`, `/sellable-quantity`, `/commissions` |

- **개장 직후 10분간 ORDER·ORDER_INFO 가 반토막(6→3)**. 9시 동시호가 직후 몰아치는 전략이면 필수 고려.
- 응답 헤더(정상·429 공통): `X-RateLimit-Limit`(현재 burst capacity) · `X-RateLimit-Remaining`(남은 토큰, 429 시 0) · `X-RateLimit-Reset`(토큰 1개 재충전 예상 초) · `Retry-After`(429 에만).
- **429 대응 (공식 권장 3원칙)**:
  1. `Retry-After`(초) 헤더 값만큼 **대기 후 재시도**.
  2. **지수 백오프**(1s → 2s → 4s …) + **jitter** 함께 적용.
  3. `X-RateLimit-Remaining` 이 낮아지면 429 나기 *전에* **선제적으로 송신 속도 완화**.
  - 한도 수치는 사전 공지 없이 조정될 수 있음 → **헤더가 source of truth**(표의 숫자를 상수로 하드코딩 금지).

---

## 4. 엔드포인트 레퍼런스

표기: `메서드 경로` — 요약 · **그룹** · (헤더) · 파라미터 → **result 타입**. ☆ = `X-Tossinvest-Account` 필요.

### 4.1 인증
- `POST /oauth2/token` — 토큰 발급 · **AUTH**. body(form): `OAuth2TokenRequest` → `OAuth2TokenResponse`. (§1)

### 4.2 시세 (Market Data)
- `GET /orderbook` — 호가 · **MARKET_DATA**. `symbol`(req) → `OrderbookResponse` { timestamp?, currency, asks[], bids[] } (각 `{price, volume}`, asks=낮은가순/bids=높은가순).
- `GET /prices` — 현재가 · **MARKET_DATA**. `symbols`(req, 콤마 최대 200) → `PriceResponse[]` { symbol, timestamp?, lastPrice, currency }.
- `GET /trades` — 최근 체결 · **MARKET_DATA**. `symbol`(req), `count`(opt, 1~50, 기본 50) → `Trade[]` { price, volume, timestamp, currency }.
- `GET /price-limits` — 상/하한가 · **MARKET_DATA**. `symbol`(req) → `PriceLimitResponse` { timestamp, upperLimitPrice?, lowerLimitPrice?, currency } (US 등 제한 없으면 null).
- `GET /candles` — 캔들 · **MARKET_DATA_CHART**. `symbol`(req), `interval`(req, **`1m`|`1d`**), `count`(opt 1~200, 기본 100), `before`(opt date-time, 과거 페이징), `adjusted`(opt bool, 기본 true) → `CandlePageResponse` { candles[], nextBefore? }. Candle = { timestamp, openPrice, highPrice, lowPrice, closePrice, volume, currency }.

### 4.3 종목 정보 (Stock Info)
- `GET /stocks` — 종목 기본정보 · **STOCK**. `symbols`(req, 콤마 최대 200) → `StockInfo[]`.
  - StockInfo: symbol, name(한글), englishName, isinCode, **market**(`KOSPI|KOSDAQ|NYSE|NASDAQ|AMEX|KR_ETC|US_ETC`), **securityType**(`STOCK|FOREIGN_STOCK|DEPOSITARY_RECEIPT|INFRASTRUCTURE_FUND|REIT|ETF|FOREIGN_ETF|ETN|STOCK_WARRANTS`), isCommonShare(보통주 여부), **status**(`SCHEDULED|ACTIVE|DELISTED`), currency, listDate?, delistDate?, sharesOutstanding, leverageFactor?(ETF/ETN), koreanMarketDetail?(국내만: liquidationTrading, nxtSupported, krxTradingSuspended, nxtTradingSuspended?).
- `GET /stocks/{symbol}/warnings` — 매수 유의사항 · **STOCK**. → `StockWarning[]` { **warningType**(`LIQUIDATION_TRADING|OVERHEATED|INVESTMENT_WARNING|INVESTMENT_RISK|VI_STATIC_AND_DYNAMIC|VI_STATIC|VI_DYNAMIC|STOCK_WARRANTS`), exchange?(KRX/NXT), startDate?, endDate? }.

### 4.4 시장 정보 (Market Info)
- `GET /exchange-rate` — 환율 · **MARKET_INFO**. `baseCurrency`(req `KRW|USD`), `quoteCurrency`(req `KRW|USD`), `dateTime`(opt) → `ExchangeRateResponse` { baseCurrency, quoteCurrency, rate(매수환율), midRate(매매기준율), basisPoint, rateChangeType(`UP|EQUAL|DOWN`), validFrom, validUntil }. **1분 갱신, 참고용** — 실제 거래 환율과 다를 수 있음.
- `GET /market-calendar/KR` — 국내 장운영 · **MARKET_INFO**. `date`(opt YYYY-MM-DD) → `KrMarketCalendarResponse` { today, previousBusinessDay, nextBusinessDay } 각 `KrMarketDay`{date, integrated?}. integrated(`IntegratedHour`) = preMarket?/regularMarket?/afterMarket? 세션(각 startTime/endTime/단일가구간). **KRX+NXT 통합 기준, 특수장 제외.**
- `GET /market-calendar/US` — 해외 장운영 · **MARKET_INFO**. `date`(opt) → `UsMarketCalendarResponse` (today/prev/next). `UsMarketDay` = date + **4 세션** dayMarket?/preMarket?/regularMarket?/afterMarket?(각 startTime/endTime). 휴장이면 4 세션 모두 null. **모든 시간 KST 표기.**

### 4.5 계좌·자산
- `GET /accounts` — 계좌 목록 · **ACCOUNT**(토큰만, 계좌헤더 불필요) → `Account[]` { accountNo, **accountSeq**(int, 헤더값), accountType(`BROKERAGE|…`) }. 없으면 빈 배열.
- ☆ `GET /holdings` — 보유 주식 · **ASSET**. `symbol`(opt 필터) → `HoldingsOverview` { totalPurchaseAmount, marketValue, profitLoss, dailyProfitLoss, **items[]** }.
  - 합산 금액은 `Price`{ krw, usd? } — **통화별 분리 합산(환산 안 함)**. overview 의 rate/rateAfterCost 는 전체를 원화환산한 소수비율(0.1516=15.16%).
  - `HoldingsItem`: symbol, name, marketCountry(`KR|US`), currency, quantity, lastPrice, averagePurchasePrice, **marketValue**{purchaseAmount, amount, amountAfterCost}, **profitLoss**{amount, amountAfterCost, rate, rateAfterCost}, **dailyProfitLoss**{amount, rate}, **cost**{commission, tax?}. ★ `amountAfterCost`/`rateAfterCost` = 수수료·세금 차감 후 — 직접 계산 불필요.

### 4.6 주문 (Order)
- ☆ `POST /orders` — 주문 생성 · **ORDER**. body: `OrderCreateRequest`(아래 §5) → `OrderResponse` { orderId, clientOrderId? }. 에러: 400/401/**409 중복(request-in-progress)**/422 비즈니스규칙/500.
- ☆ `POST /orders/{orderId}/modify` — 정정 · **ORDER**. body: `OrderModifyRequest` → `OrderOperationResponse` { **orderId**(정정으로 새로 발급, 원주문과 다름) }. 409 정정불가/422.
- ☆ `POST /orders/{orderId}/cancel` — 취소 · **ORDER**. body: `{}` (빈 객체) → `OrderOperationResponse` { orderId(새 발급) }. 이미 체결된 주문 취소 불가(409).

### 4.7 주문 조회 (Order History)
- ☆ `GET /orders` — 주문 목록 · **ORDER_HISTORY**. `status`(req **`OPEN`** — PENDING/PARTIAL_FILLED/PENDING_CANCEL/PENDING_REPLACE 반환), `symbol`(opt), `from`/`to`(opt), `cursor`(opt), `limit`(opt 1~100, 기본 20) → `PaginatedOrderResponse` { orders[], nextCursor?, hasNext }. ⚠ **`status=CLOSED` 는 현재 `400 closed-not-supported`** (미지원). `OPEN` 은 nextCursor 항상 null/hasNext false.
- ☆ `GET /orders/{orderId}` — 주문 상세 · **ORDER_HISTORY**. → `Order`(모든 상태 조회 가능, §5).

### 4.8 거래 가능 정보 (Order Info)
- ☆ `GET /buying-power` — 매수가능금액 · **ORDER_INFO**. `currency`(req `KRW|USD`) → `BuyingPowerResponse` { currency, **cashBuyingPower**(현금 기반, 미수 미발생) }. KRW=정수/USD=소수.
- ☆ `GET /sellable-quantity` — 매도가능수량 · **ORDER_INFO**. `symbol`(req) → `SellableQuantityResponse` { sellableQuantity }. KR=정수/US=소수 가능.
- ☆ `GET /commissions` — 매매수수료 · **ORDER_INFO**. → `Commission[]` { marketCountry(`KR|US`), commissionRate(%, 0.015=0.015%), startDate?, endDate? }.

---

## 5. 주문 스키마 디테일 (제일 조심할 곳)

### OrderCreateRequest — **oneOf 두 변형**

**(A) 수량 기반 (`OrderCreateQuantityBased`)** — KR·US 공용:
```json
{
  "symbol": "005930", "side": "BUY", "orderType": "LIMIT",
  "price": "70000", "quantity": "10",
  "timeInForce": "DAY", "clientOrderId": "my-order-001"
}
```
- `side`: `BUY|SELL`. `orderType`: `LIMIT|MARKET`. `timeInForce`: `DAY|CLS`(기본 DAY). `LIMIT`+`CLS`=LOC. `CLS`(장마감주문)는 현재 **US + 특정 orderType** 한정.
- `price`: **LIMIT 필수 / MARKET 전달 시 400**. KR 은 정수(원)이고 **호가 단위(tick size) 정합** 필요(틀리면 `invalid-tick-size`/`invalid-request`).
- `quantity`: **정수만**(소수점 주문은 (B) 사용).

**(B) 금액 기반 (`OrderCreateAmountBased`)** — **US 시장가 전용**:
```json
{ "symbol": "AAPL", "side": "BUY", "orderType": "MARKET", "orderAmount": "100" }
```
- `orderType` **`MARKET` 만**. `orderAmount`(달러) 확정 → 체결 수량 변동(소수점 주식). **정규장 시간에만 접수**(외 시간 `422 amount-order-outside-regular-hours`).

**공통 옵션**:
- `clientOrderId`(opt, 최대 36자 `[A-Za-z0-9_-]`): **멱등성 키**. 동일 값 재요청 시 이전 결과 그대로 반환. **10분간 유효**(이후 동일 값은 새 주문). 서버 자동생성 안 함 → **자동매매면 직접 부여 강권**(네트워크 단절 시 중복주문 방지).
- `confirmHighValueOrder`(bool, 기본 false): **1억원 이상** 주문은 `true` 아니면 `400 confirm-high-value-required`. **30억원 이상**은 이 플래그와 무관하게 `422 max-order-amount-exceeded`.

### OrderModifyRequest
- `orderType`(req `LIMIT|MARKET`), `price`(LIMIT 필수), `confirmHighValueOrder`.
- `quantity`: **KR 필수(양의 정수)** / **US 전달 불가**(주면 `400 us-modify-quantity-not-supported`, US 는 가격 정정만).

### Order (조회 결과)
- orderId, symbol, side, orderType, **timeInForce**(`DAY|CLS|OPG` — OPG=장개시주문, 현재 미지원), **status**(`OrderStatus`), price?(MARKET 시 null), quantity, orderAmount?(US 금액주문만), currency, orderedAt, canceledAt?, **execution**(`OrderExecution`).
- `OrderExecution`: filledQuantity, averageFilledPrice?, filledAmount?, commission?, tax?, filledAt?, settlementDate?(결제예정일).
- **`OrderStatus` 10종**: `PENDING`(체결대기) · `PENDING_CANCEL` · `PENDING_REPLACE`(정정대기) · `PARTIAL_FILLED` · `FILLED` · `CANCELED` · `REJECTED` · `CANCEL_REJECTED` · `REPLACE_REJECTED` · `REPLACED`.

---

## 6. 에러 코드 전체 표

`code` 기준 분기. HTTP status 와 함께:

| HTTP | code | 의미 |
|---|---|---|
| 400 | `invalid-request` | 호가유형·방향·수량·금액·필수 파라미터 등 잘못된 요청(포괄) |
| 400 | `confirm-high-value-required` | 1억↑ 주문인데 `confirmHighValueOrder!=true` |
| 400 | `account-header-required` | `X-Tossinvest-Account` 헤더 누락 |
| 400 | `closed-not-supported` | `GET /orders?status=CLOSED` 현재 미지원 |
| 400 | `us-modify-quantity-not-supported` | US 정정에 `quantity` 전달 |
| 401 | `invalid-token` | 토큰 무효/형식 오류 |
| 401 | `expired-token` | 토큰 만료 → 재발급 |
| 401 | `edge-blocked` | `Authorization` 헤더 누락(앞단 차단) |
| 401 | `login-user-not-found` | 토큰 대응 로그인 정보 없음 |
| 403 | `forbidden` / `edge-blocked` | 권한 부족 / 비허용 요청 |
| 404 | `stock-not-found` · `exchange-rate-not-found` · `account-not-found` · `order-not-found` · `edge-blocked`(미지원 경로) | 대상 없음 |
| 409 | `request-in-progress` | 동일 `clientOrderId` 생성 요청 처리 중 |
| 409 | `already-filled` · `already-canceled` · `already-modified` · `already-rejected` · `already-processing` | 정정/취소 대상 상태 충돌 |
| 422 | `insufficient-buying-power` | 매수가능금액 부족 |
| 422 | `order-hours-closed` | 접수 불가 시간 |
| 422 | `stock-restricted` · `price-out-of-range` · `opposite-pending-order-exists` · `order-type-not-allowed` · `prerequisite-required`(약관/위험고지 미충족) | 비즈니스 규칙 |
| 422 | `market-not-supported-for-stock`(KR) · `investor-exchange-not-integrated`(KR, SOR 미설정) · `amount-order-outside-regular-hours`(US) · `modify-restricted` · `cancel-restricted` | 시장/주문 제약 |
| 429 | `rate-limit-exceeded` / `edge-rate-limit-exceeded` | TPS 초과 → `Retry-After` |
| 500 | `internal-error` / `maintenance` | 일시 장애 / 점검 |

---

## 7. ⚠ 함정 모음 (모르면 당하는 것 한눈에)

*상세는 각 절. 여기는 "이거 몰라서 한 번씩 깨지는" 것만 모음.*

- **토큰 엔드포인트만 응답 포맷이 다르다** — 성공은 OAuth2 표준(`access_token`…, `result` 래핑 안 함), 실패는 `{error, error_description}`. 나머지 전 API 의 `result`/`ErrorResponse` envelope 과 **분기 따로**. (§1·§2)
- **성공 payload 는 `result` 안에 있다** — 최상위가 데이터가 아님. `res.result` 언래핑 안 하면 전부 undefined. (§2)
- **계좌 헤더 누락 = `400 account-header-required`** — 계좌·자산·주문은 전부 `X-Tossinvest-Account` 필수. 시세류만 토큰으로 됨. (§1)
- **`ACCOUNT` 그룹 초당 1회** — `/accounts` 를 매 요청 부르면 즉시 429. accountSeq **한 번 받아 캐싱**. (§3)
- **개장 직후 10분(09:00~09:10) `ORDER`·`ORDER_INFO` 반토막**(6→3/s) — 9시 동시호가 전략은 반드시 반영. (§3)
- **429 는 헤더가 진실** — `Retry-After` 대기 + 백오프, 한도 숫자는 공지 없이 바뀜(상수 금지). (§3)
- **돈·수량은 전부 문자열 decimal** — `number` 캐스팅 시 반올림으로 금액 틀어짐. Decimal 로 다뤄라. (§2)
- **멱등성은 직접 챙긴다** — `clientOrderId` 줘야만 적용, **10분만 유효**, 서버 자동생성 안 함. 자동매매면 필수(단절 시 중복주문 방지). (§5)
- **고액주문 확인 플래그** — 1억원↑ 은 `confirmHighValueOrder=true` 아니면 `400`, **30억원↑ 은 플래그 무관 `422`**. (§5)
- **US 금액주문은 정규장 + `MARKET` 전용** / **US 정정은 가격만**(quantity 주면 `400 us-modify-quantity-not-supported`). (§4.6·§5)
- **정정·취소는 새 `orderId` 를 반환** — 원주문 id 와 다름. 추적 매핑이 끊기지 않게 연결해둬라. (§4.6)
- **`GET /orders?status=CLOSED` 현재 미지원**(`400 closed-not-supported`) — 지금은 `OPEN` 만. (§4.7)
- **KR 지정가 tick size 정합** — 호가 단위 안 맞으면 `400`(예: 5만~20만원 구간 100원 단위). (§5)
- **실시간 스트리밍 없음** — WebSocket 미지원, 폴링뿐. 시세는 `/prices` 다건(최대 200) 묶어 MARKET_DATA 10/s 안에서. (서론·§3)
- **unknown enum/code 관용 구현** — 서버가 값 추가 가능. 모르는 enum/에러코드에 안 깨지게(문서가 명시 요구). (§2)

## 8. 새 프로젝트 설계 체크리스트

- **토큰 매니저**: `expires_in` 만료 전 갱신 + 메모리 캐싱. 401 `expired-token` 시 1회 재발급 후 재시도.
- **accountSeq 캐싱**: 부팅 시 `/accounts` 1회 → 보관(ACCOUNT 1/s).
- **decimal 안전**: 금액/수량은 문자열 그대로 받아 Decimal 로. JS 면 `number` 변환 금지.
- **주문은 항상 `clientOrderId` 부여**(멱등성 10분). 생성 응답 못 받으면 같은 키로 재요청 → 중복 방지.
- **주문 전 사전조회**: `buying-power`/`sellable-quantity` 로 거부 왕복 줄이기. 단 ORDER_INFO 도 피크시간 3/s.
- **레이트리미터**: 그룹별 토큰버킷 클라이언트단 구현 + 429 시 `Retry-After`+백오프+jitter. 9~9:10 ORDER/ORDER_INFO 반토막 반영.
- **실시간 없음**: 시세는 폴링(`/prices` 다건 200개 묶기) — MARKET_DATA 10/s 안에서 배치.
- **장운영 캘린더**로 휴장/세션 판단(하드코딩 금지). KR=KRX+NXT 통합, US=4세션 nullable, **시간은 KST**.
- **unknown enum/code 관용**: 새 값 들어와도 안 깨지게(문서 명시).
- **상태머신**: OrderStatus 10종 + PENDING_*(취소/정정 대기) 전이 처리. 정정/취소는 **새 orderId 반환**(원주문 추적 끊기지 않게 매핑).

---

> 📌 **이 레포에서의 구현 현황**: SDK(`pytossinvest`)는 §1~§5 의 인증·레이트리밋·decimal·에러·엔드포인트를 구현 완료(MIT). MCP 서버(`pytossinvest-mcp`)는 그 위에 안전모델(모드·가드레일·preview→confirm·멱등성)을 얹음(Apache-2.0). 설계·구현 상세는 `docs/superpowers/` 의 spec/plan, 운영 컨벤션은 루트 `CLAUDE.md` 참고.
