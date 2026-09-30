> **언제 읽나**: 이 레포(`pytossinvest` SDK + `pytossinvest-mcp` MCP 서버)가 다루는 **토스증권 Open API 의 코어 레퍼런스**. 인증 2단 구조·엔드포인트·요청/응답 스키마·rate limit·에러코드·도메인 함정의 단일 소스. **SDK 엔드포인트 추가·MCP 툴 작업·주문 로직 손대기 전 여기부터 읽는다.** (원래 블로그(zerry.co.kr) `docs/wiki/` 에 있다가 이 레포로 이전됨 — 블로그 글감: `tossinvest-open-api-guide`.)
>
> **🔄 자가갱신**: 이 문서는 **스냅샷**이다. canonical `openapi.json` 과 어긋나거나, 코드 작업 중 새 필드·엔드포인트·enum·함정을 발견하면 **그 세션에서 바로 이 문서를 갱신**한다(커밋은 수동). 권위 순서는 ① openapi.json → ② developers.tossinvest.com/docs → ③ 본 문서.

# 토스증권 Open API 레퍼런스

- **출처(canonical)**: `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json` (OpenAPI **3.1.0**, `info.version` **1.2.19**)
- **문서 허브**: https://developers.tossinvest.com/docs · AI/평문용 `https://developers.tossinvest.com/llms.txt` · 개요 `…/openapi-docs/overview.md`
- **Base URL**: `https://openapi.tossinvest.com` (모든 경로 prefix)
- **연동**: REST + **WebSocket**(실시간 체결·호가·본인 주문 이벤트, §4.10). WebSocket 스펙은 REST 와 별도 버전(AsyncAPI 3.0, `info.version` 1.2.2): `https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json`.
- **검증 시점**: 2026-09-30(canonical 재확인, 1.1.1 → 1.2.19 갱신). ⚠ 한도·정책·엔드포인트는 사전 공지 없이 바뀔 수 있다.
- **이 문서는 스냅샷이다 — 막히거나 의심되면 반드시 공식 문서를 본다**: 권위 순서는 ① openapi.json(canonical 스펙) → ② [developers.tossinvest.com/docs](https://developers.tossinvest.com/docs)(인터랙티브) → ③ 본 문서. 이 문서에 없는 필드·새 엔드포인트·세부 enum·정확한 한도 수치는 위 openapi.json 을 source of truth 로 삼아 재확인. (LLM/스크립트면 `…/openapi-docs/overview.md` + `…/api-reference/README.md` 가 평문이라 파싱 쉽다.)

## 0. 자격 / 키 발급 (시점 한정 정보)

- 대상: **토스증권 계좌 보유자**. canonical overview 는 사전신청/대기자 단계 없이 **WTS 에서 직접 키 발급**만 안내(아래) — 2026-06-17 스냅샷의 "약관동의→본인인증→사전신청→순차 오픈 알림" 게이트는 더는 문서에 없음(2026-06-19 재확인).
- **토스증권 WTS(PC 웹)** 로그인 → `설정 > Open API` 메뉴에서 `client_id` / `client_secret` 발급(셀프서비스).
- 국내주식 수수료 2026-06 까지 면제 프로모션 안내가 있었음(시점 한정, 신청 시 재확인).
- 현재 `accounts` 는 **종합매매(BROKERAGE) 계좌만** 반환. 자녀계좌 사용 불가. (`accountType` enum 은 `BROKERAGE|OVERSEAS_DERIVATIVES|PENSION_SAVINGS|RESHORING_INVESTMENT` 4종으로 정의됐으나 노출은 BROKERAGE 뿐.)
- **허용 IP 등록 필수**: 설정 > Open API 하단 **허용 IP 관리** 에 등록하지 않은 IP 의 호출은 `403` 차단(REST·WebSocket 공통). 2026-09-30 overview 의 시작하기 2단계.

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
  - `access_token`: JWT. `expires_in`: 만료까지 남은 **초**(값은 발급해봐야 확정). refresh token 없음(만료 시 재발급).
  - **client 당 유효 토큰 1개** — 재발급하면 이전 토큰은 즉시 무효(`401 token-revoked`). 프로세스 여러 개가 같은 client 로 각자 발급하면 서로 토큰을 죽인다.
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
- **페이지네이션**: cursor 방식은 `GET /orders`(CLOSED 만)·`GET /conditional-orders` — 응답 `nextCursor`/`hasNext`, 다음 호출에 `cursor` 전달. 캔들(종목·시장지표)은 `nextBefore`(→ `before`), 수급 동향 5종·지표 투자자별 매매대금은 `nextUntil`(→ `until`, YYYY-MM-DD inclusive)로 과거 페이징.
- **심볼**: KR = 6자리 종목코드 — **숫자 또는 영문·숫자 조합**(`005930`·`0101N0`), US = 영문 티커(`AAPL`). 허용 문자는 영문 대/소문자·숫자·`.`·`-`. 다건 조회(prices·stocks·market-indicators/prices)는 콤마 구분 **최대 200개**.

---

## 3. Rate Limits

**클라이언트 × API 그룹** 단위 초당 요청 수(TPS) 제한. 각 엔드포인트 description 끝에 소속 그룹이 적혀 있다.

| 그룹 | 한도 | 피크(09:00~09:10 KST) | 소속 엔드포인트 |
|---|---|---|---|
| `AUTH` | 5/s | — | `POST /oauth2/token` |
| `ACCOUNT` | **1/s** | — | `GET /accounts` |
| `ASSET` | 5/s | — | `GET /holdings` |
| `STOCK` | 5/s | — | `GET /stocks`, `…/warnings` |
| `STOCK_ALL` | **1/s** | — | `GET /stocks/all` |
| `STOCK_TRADING_TREND` | 10/s | — | `/stocks/{symbol}/investor-trading·program-trades·short-selling·credit-trades·securities-lending` |
| `MARKET_INFO` | 3/s | — | `GET /exchange-rate`, `/market-calendar/*` |
| `MARKET_DATA` | **15/s** | — | `GET /orderbook`, `/prices`, `/trades`, `/price-limits` |
| `MARKET_DATA_CHART` | **20/s** | — | `GET /candles` |
| `RANKING` | 5/s | — | `GET /rankings` |
| `MARKET_INDICATOR` | 10/s | — | `GET /market-indicators/prices`, `…/{symbol}/investor-trading` |
| `MARKET_INDICATOR_CHART` | 5/s | — | `GET /market-indicators/{symbol}/candles` |
| `ORDER` | **10/s** | **10/s**(반토막 없음) | `POST /orders`, `…/modify`, `…/cancel` |
| `ORDER_HISTORY` | 5/s | — | `GET /orders`, `/orders/{orderId}` |
| `ORDER_INFO` | 6/s | **3/s** | `GET /buying-power`, `/sellable-quantity`, `/commissions` |
| `CONDITIONAL_ORDER` | 5/s | — | `POST /conditional-orders`, `…/modify`, `DELETE …/{id}` |
| `CONDITIONAL_ORDER_HISTORY` | 10/s | — | `GET /conditional-orders`, `…/{id}` |

- ⚠ 2026-09-30 재검증에서 1.1.1 대비 변경: `MARKET_DATA` 10→15, `MARKET_DATA_CHART` 5→20, `ORDER` 6→10 이고 **ORDER 피크 감쇠 소멸**. overview 표에는 `MARKET_INDICATOR_PRICE` 그룹도 있으나 openapi.json 의 `market-indicators/prices` description 은 `MARKET_INDICATOR` 로 표기(불일치 — 헤더가 진실).
- WebSocket 은 별도 한도(§4.10).
- **개장 직후 10분간 `ORDER_INFO` 만 반토막(6→3)**. (1.1.1 에선 ORDER 도 반토막이었으나 지금은 10/s 유지.) 9시 동시호가 직후 몰아치는 전략이면 필수 고려.
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
- `GET /rankings` — 주식 랭킹 · **RANKING**. `type`(req `MARKET_TRADING_AMOUNT|MARKET_TRADING_VOLUME|TOP_GAINERS|TOP_LOSERS|TOSS_SECURITIES_TRADING_AMOUNT|TOSS_SECURITIES_TRADING_VOLUME`), `marketCountry`(req `KR|US`), `duration`(req `realtime|1d|1w|1mo|3mo|6mo|1y`, 거래일 기준), `excludeInvestmentCaution`(opt bool, 기본 false), `count`(opt 1~100, 기본 100) → { rankedAt?, rankings[] } 각 { rank, symbol, currency, price{lastPrice, basePrice, changeRate?}, tradingVolume, tradingAmount }. `TOP_GAINERS|TOP_LOSERS` 는 `realtime` 불가(`400 unsupported-ranking-duration`). `TOSS_SECURITIES_*` 는 토스증권 체결 기준, 나머지는 시장 전체. `basePrice`/`changeRate` 는 TOP_* 만 기간 시작 기준, 나머지는 항상 전일 기준. 미집계 조합·시세 실패 종목은 에러 아닌 빈/짧은 배열(`rankedAt` null).

### 4.2b 시장 지표 (Market Indicators) — 토큰만, 계좌헤더 불필요
심볼 카탈로그 8종만 지원(그 외 `400 unsupported-symbol`): `KOSPI`·`KOSDAQ`(지수, 포인트), `KR_BOND_2Y|3Y|5Y|10Y|20Y|30Y`(국채 수익률 %, 예 `3.25`). 개별 종목은 이 그룹이 아니라 `/prices`·`/candles`.
- `GET /market-indicators/prices` — 현재가 · **MARKET_INDICATOR**. `symbols`(req, 콤마 최대 200) → [{ symbol, timestamp?, lastPrice }] (currency 필드 없음).
- `GET /market-indicators/{symbol}/candles` — 캔들 · **MARKET_INDICATOR_CHART**. `interval`(req `1m|1d`), `count`(opt 1~200, 기본 100), `before`(opt) → { candles[], nextBefore? } (Candle 은 currency 없음, **최신순**). **`1m` 은 KOSPI·KOSDAQ 만**, 국채는 `1d` 만(분봉 요청 시 `400 invalid-request`). `adjusted` 파라미터 없음.
- `GET /market-indicators/{symbol}/investor-trading` — 투자자별 매매대금 · **MARKET_INDICATOR**. `symbol`(path `KOSPI|KOSDAQ`), `interval`(req `1d|1w|1mo|1y`), `count`(opt 1~100, 기본 10), `until`(opt date) → { nextUntil?, records[] } 각 { date, updatedAt, individual, foreigner, institution{…, breakdown 7분류}, otherCorporation } 전부 { buyAmount, sellAmount }(순매수 필드 없음, 등록·미등록 외국인 합계).

### 4.3 종목 정보 (Stock Info)
- `GET /stocks` — 종목 기본정보 · **STOCK**. `symbols`(req, 콤마 최대 200) → `StockInfo[]`.
  - StockInfo: symbol, name(한글), englishName, isinCode, **market**(`KOSPI|KOSDAQ|NYSE|NASDAQ|AMEX|KR_ETC|US_ETC`), **securityType**(`STOCK|FOREIGN_STOCK|DEPOSITARY_RECEIPT|INFRASTRUCTURE_FUND|REIT|ETF|FOREIGN_ETF|ETN|STOCK_WARRANTS`), isCommonShare(보통주 여부), **status**(`SCHEDULED|ACTIVE|DELISTED`), currency, listDate?, delistDate?, sharesOutstanding, leverageFactor?(ETF/ETN), koreanMarketDetail?(국내만: liquidationTrading, nxtSupported, krxTradingSuspended, nxtTradingSuspended?).
- `GET /stocks/all` — 마켓별 전체 종목 · **STOCK_ALL(1/s)**. `market`(req `KOSPI|KOSDAQ|NYSE|NASDAQ|AMEX|KR_ETC|US_ETC`), `status`(opt, 기본 `ACTIVE`), `securityType`(opt), `commonShare`(opt bool) → [{ symbol, name, securityType, isCommonShare, isinCode }]. symbol 오름차순, **페이지네이션 없이 전량**(NASDAQ 약 2,800건). 일 배치 갱신 → **하루 1회 조회 후 캐싱 권장**. 상세(통화·상장상태 등)는 `/stocks` 로 채움.
- `GET /stocks/{symbol}/warnings` — 매수 유의사항 · **STOCK**. → `StockWarning[]` { **warningType**(`LIQUIDATION_TRADING|OVERHEATED|INVESTMENT_WARNING|INVESTMENT_RISK|VI_STATIC_AND_DYNAMIC|VI_STATIC|VI_DYNAMIC|STOCK_WARRANTS`), exchange?(KRX/NXT), startDate?, endDate? }.

#### 수급 동향 (국내 KR 종목 전용, 모두 **STOCK_TRADING_TREND** · 토큰만)
공통: `symbol`(path), `count`(opt 1~100, 기본 10), `until`(opt date, inclusive) → `{ nextUntil?, records[] }` 최신순, 다음 페이지는 `until=nextUntil`. 타 시장 종목은 `400 unsupported-market`. 수량은 주식 수, KRX+NXT 통합. 당일은 장중 잠정치.
- `GET /stocks/{symbol}/investor-trading` — 투자자별 매매동향. record: { date, updatedAt, individual?, foreigner, institution{ buy/sell/netBuyVolume, breakdown?(financialInvestment·insurance·trust·privateEquityFund·bank·otherFinancialInstitution·pensionFund) }, otherCorporation?, foreignerHolding?{holdingQuantity, limitQuantity, holdingRate}, cfd?{buy/sellBalanceQuantity·Rate} }. 투자자 블록 = { buyVolume, sellVolume, netBuyVolume }. 잠정 시점엔 individual·breakdown·otherCorporation·foreignerHolding·cfd 가 null(확정치는 저녁, CFD 는 T+1 새벽). `foreigner` 는 등록외국인 기준(지표 API 와 기준 다름).
- `GET /stocks/{symbol}/program-trades` — 프로그램매매. record: { date, arbitrage, nonArbitrage } 각 { buyVolume, sellVolume, netBuyVolume }.
- `GET /stocks/{symbol}/short-selling` — 공매도. record: { date, updatedAt, shortSellingVolume, shortSellingAmount, shortSellingVolumeRate?, shortSellingAmountRate? }.
- `GET /stocks/{symbol}/credit-trades` — 신용거래. record: { date, updatedAt, marginLoan?(융자), stockLoan?(대주) } 각 { newQuantity, returnQuantity, balanceQuantity, balanceRate, tradingRate }.
- `GET /stocks/{symbol}/securities-lending` — 대차거래. record: { date, updatedAt, executionQuantity, repaymentQuantity, balanceQuantity, balanceAmount }.

### 4.4 시장 정보 (Market Info)
- `GET /exchange-rate` — 환율 · **MARKET_INFO**. `baseCurrency`(req `KRW|USD`), `quoteCurrency`(req `KRW|USD`), `dateTime`(opt) → `ExchangeRateResponse` { baseCurrency, quoteCurrency, rate(매수환율), midRate(매매기준율), basisPoint, rateChangeType(`UP|EQUAL|DOWN`), validFrom, validUntil }. **1분 갱신, 참고용** — 실제 거래 환율과 다를 수 있음.
- `GET /market-calendar/KR` — 국내 장운영 · **MARKET_INFO**. `date`(opt YYYY-MM-DD) → `KrMarketCalendarResponse` { today, previousBusinessDay, nextBusinessDay } 각 `KrMarketDay`{date, integrated?}. integrated(`IntegratedHour`) = preMarket?/regularMarket?/afterMarket? 세션(각 startTime/endTime/단일가구간). **KRX+NXT 통합 기준, 특수장 제외.**
- `GET /market-calendar/US` — 해외 장운영 · **MARKET_INFO**. `date`(opt) → `UsMarketCalendarResponse` (today/prev/next). `UsMarketDay` = date + **4 세션** dayMarket?/preMarket?/regularMarket?/afterMarket?(각 startTime/endTime). 휴장이면 4 세션 모두 null. **모든 시간 KST 표기.**

### 4.5 계좌·자산
- `GET /accounts` — 계좌 목록 · **ACCOUNT**(토큰만, 계좌헤더 불필요) → `Account[]` { accountNo, **accountSeq**(int, 헤더값), accountType(`BROKERAGE|OVERSEAS_DERIVATIVES|PENSION_SAVINGS|RESHORING_INVESTMENT`, 현재 노출은 BROKERAGE 뿐) }. 없으면 빈 배열.
- ☆ `GET /holdings` — 보유 주식 · **ASSET**. `symbol`(opt 필터) → `HoldingsOverview` { totalPurchaseAmount, marketValue, profitLoss, dailyProfitLoss, **items[]** }.
  - 합산 금액은 `Price`{ krw, usd? } — **통화별 분리 합산(환산 안 함)**. overview 의 rate/rateAfterCost 는 전체를 원화환산한 소수비율(0.1516=15.16%).
  - `HoldingsItem`: symbol, name, marketCountry(`KR|US`), currency, quantity, lastPrice, averagePurchasePrice, **marketValue**{purchaseAmount, amount, amountAfterCost}, **profitLoss**{amount, amountAfterCost, rate, rateAfterCost}, **dailyProfitLoss**{amount, rate}, **cost**{commission, tax?}. ★ `amountAfterCost`/`rateAfterCost` = 수수료·세금 차감 후 — 직접 계산 불필요.

### 4.6 주문 (Order)
- ☆ `POST /orders` — 주문 생성 · **ORDER**. body: `OrderCreateRequest`(아래 §5) → `OrderResponse` { orderId, clientOrderId? }. 에러: 400/401/**409 중복(request-in-progress)**/422 비즈니스규칙/500.
- ☆ `POST /orders/{orderId}/modify` — 정정 · **ORDER**. body: `OrderModifyRequest` → `OrderOperationResponse` { **orderId**(정정으로 새로 발급, 원주문과 다름) }. 409 정정불가/422.
- ☆ `POST /orders/{orderId}/cancel` — 취소 · **ORDER**. body: `{}` (빈 객체) → `OrderOperationResponse` { orderId(새 발급) }. 이미 체결된 주문 취소 불가(409).

### 4.7 주문 조회 (Order History)
- ☆ `GET /orders` — 주문 목록 · **ORDER_HISTORY**. `status`(req `OPEN|CLOSED`), `symbol`(opt), `from`/`to`(opt date, `orderedAt` KST 기준), `cursor`(opt), `limit`(opt 1~100, 기본 20) → `PaginatedOrderResponse` { orders[], nextCursor(nullable), hasNext }. `status` 는 `orders[].status` 를 **그룹화한 라벨**(값 체계 다름): `OPEN` = PENDING/PARTIAL_FILLED/PENDING_CANCEL/PENDING_REPLACE, `CLOSED` = FILLED/CANCELED/REJECTED/REPLACED/CANCEL_REJECTED/REPLACE_REJECTED(+PARTIAL_FILLED). **`OPEN` 은 전량 반환하고 `limit`·`cursor` 무시**(`from`/`to` 만 적용). **`CLOSED` 는 지원됨**(1.1.1 의 `400 closed-not-supported` 소멸 — spec 에 코드 자체 없음)이고 `limit`·`cursor`·`from`/`to` 모두 적용. Open API 로 못 내는 호가유형(장전·장후 시간외 종가 등)의 주문은 목록·상세 모두에서 조회되지 않음.
- ☆ `GET /orders/{orderId}` — 주문 상세 · **ORDER_HISTORY**. → `Order`(모든 상태 조회 가능, §5).

### 4.8 거래 가능 정보 (Order Info)
- ☆ `GET /buying-power` — 매수가능금액 · **ORDER_INFO**. `currency`(req `KRW|USD`) → `BuyingPowerResponse` { currency, **cashBuyingPower**(현금 기반, 미수 미발생) }. KRW=정수/USD=소수.
- ☆ `GET /sellable-quantity` — 매도가능수량 · **ORDER_INFO**. `symbol`(req) → `SellableQuantityResponse` { sellableQuantity }. KR=정수/US=소수 가능.
- ☆ `GET /commissions` — 매매수수료 · **ORDER_INFO**. → `Commission[]` { marketCountry(`KR|US`), commissionRate(%, 0.015=0.015%), startDate?, endDate? }.

### 4.9 조건주문 (Conditional Order) — 전부 ☆ 계좌헤더 필요
감시가 도달 시 자동으로 주문 생성. **이 API 로 만든 것 외에 앱 등 다른 채널 조건주문도 조회에 섞여 나옴**(`type` 으로 구분). 발동 세션: KR = KRX 정규장만, US = 거래 가능한 전 시간대.
- ☆ `POST /conditional-orders` — 등록 · **CONDITIONAL_ORDER**. body: { symbol, type(`SINGLE|OCO|OTO`), quantity(그룹 공통), orderType(`LIMIT|MARKET`, 그룹 공통), clientOrderId?(멱등), expireDate(req YYYY-MM-DD), first{ orderSide, triggerPrice, orderPrice? }, second?(SINGLE 생략·OCO/OTO 필수), confirmHighValueOrder? } → { conditionalOrderId, clientOrderId? }. 규칙: `LIMIT` 이면 각 조건 `orderPrice` 필수 / `MARKET` 이면 지정 불가. **OCO**: 둘 다 SELL, `first` 감시가 > 현재가 > `second` 감시가, LIMIT 만. **OTO**: first=BUY, second=SELL(first 체결 후 second 감시 시작), LIMIT 만. 에러 400/404/422. 응답 status 목록에 429/500 없음.
- ☆ `POST /conditional-orders/{conditionalOrderId}/modify` — 수정 · **CONDITIONAL_ORDER**. body: 등록과 같되 `symbol`·`clientOrderId` 없음, **전체 재설정**(type 전환 SINGLE→OCO 허용). → { conditionalOrderId }. ⚠ **취소+재생성 방식이라 새 id 발급, 기존 id 무효**.
- ☆ `DELETE /conditional-orders/{conditionalOrderId}` — 취소 · **CONDITIONAL_ORDER**. 성공 **204 (본문 없음, `result` envelope 없음)**. 400/404.
- ☆ `GET /conditional-orders` — 목록 · **CONDITIONAL_ORDER_HISTORY**. `status`(req `OPEN|CLOSED`), `symbol`(opt), `cursor`, `limit`(opt 1~100, 기본 20) → { conditionalOrders[], nextCursor?, hasNext }. OPEN = 응답 status ∈ WATCHING/PAUSED/ORDERING/ORDERED, CLOSED = COMPLETED/EXPIRED.
- ☆ `GET /conditional-orders/{conditionalOrderId}` — 상세 · **CONDITIONAL_ORDER_HISTORY**. 진행+종료 모두. 목록과 같은 타입.
- `ConditionalOrder`: conditionalOrderId, type, **status**(`WATCHING|PAUSED|ORDERING|ORDERED|COMPLETED|EXPIRED`), symbol, market(`KR|US`), quantity, orderType, expireDate?, createdAt, **first/second**{ type(`STOP|PROFIT_RATE`), **status**(`WATCHING|HOLDING|PAUSED|ORDERING|ORDERED|COMPLETED|EXPIRED|CANCELED`), triggerPrice?, targetProfitRate?, orderPrice?, triggeredOrderId?(발동돼 생성된 일반 주문 id) }.
- 조건주문 전용 에러: `404 conditional-order-not-found`, `422 duplicate-conditional-order`(OCO·OTO 는 종목당 1개, SINGLE 은 무제한)·`condition-already-met`(설정가가 이미 충족)·`idempotency-key-conflict`.

### 4.10 WebSocket (실시간) — 별도 스펙 AsyncAPI 3.0
- **URL** `wss://openapi-ws.tossinvest.com/ws/v1` (TLS 필수). **인증**: 핸드셰이크에 `Authorization: Bearer {access_token}`(REST 와 같은 토큰). 없음·무효·만료 `401`, 허용 IP 미등록 `403`, 서버 오류 `503`(HTTP 로만 응답).
- **연결 한도**: 계정당 동시 2개(초과 시 **새 연결 수락, 가장 오래된 연결 종료**). 연결당 구독 100건(`codes` 합산, 채널×종목 각 1건), 선언 5회/초.
- **선언형 구독**: 클라이언트가 보내는 **JSON 배열 1개 = 현재 구독 전체(full-replace)**, `[]` = 전체 해제. 원소 `{"id":"req-1"}`(ack 에 echo) 또는 `{"type":..., "codes":[...]}`.

| type | codes | 데이터 |
|---|---|---|
| `trade:us` / `trade:kr` | 종목 심볼 | { price, volume, timestamp, currency } (KR 은 KRX+NXT 통합만) |
| `orderbook:us` / `orderbook:kr` | 종목 심볼 | { timestamp?, currency, asks[], bids[] } |
| `personal:order` | `accountSeq` 문자열 | { event(`PENDING|PARTIAL_FILL|FILL|CANCELING|CANCELED|REPLACING|REPLACED|REJECTED|CANCEL_REJECTED|REPLACE_REJECTED`), accountSeq, order{ REST `Order` 와 같은 필드(단 execution 에 filledAt 없음) } } |

- **수신 프레임**(top-level `type`): `subscriptions`(ack: `subscribed[]`·`rejected[]`), `message`(`topic` = `{type}:{code}` 예 `trade:us:AAPL`, `personal:order:3` + `data`), `error`(선언 단위 실패·`server-shutdown`), `pong`.
- **keepalive**: 서버는 **클라이언트 수신이 180초 없으면 종료**(서버가 보내는 데이터는 타이머를 리셋하지 않음). **순수 텍스트 `PING`(JSON 아님) 을 60초 간격**으로 보내면 `{"type":"pong"}`. 표준 ping/pong 프레임도 지원.
- **전달 보장**: 시세(`trade`·`orderbook`)는 LOSSY(밀리면 중간 프레임 유실, sequence 없음). `personal:order` 는 LOSSLESS(세션 내 한정, 수신이 2초 이상 막히면 연결 종료) → **재연결 후 `GET /orders` 로 재동기화**.
- **에러**: `error.code` = `wrong-format|no-type|invalid-type|no-codes|too-many-topics|too-many|rate-limit-exceeded|internal-error|server-shutdown`. 항목별 거부 `rejected[].code` = `stock-not-found|symbol-market-mismatch|account-not-found`(일부 거부여도 나머지는 정상 구독, 거부 항목은 재선언 시 반복 거부되니 뺄 것). `rate-limit-exceeded` 는 ~1초 대기 후 재선언(`Retry-After` 없음).

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
- `side`: `BUY|SELL`. `orderType`: `LIMIT|MARKET`. `timeInForce`: `DAY|CLS|OPG`(기본 DAY). `LIMIT`+`CLS`=LOC. `CLS`(장마감주문)는 현재 **US + `LIMIT`** 한정. **`OPG`(장개시주문, KR 시가단일가)는 이제 주문 생성 가능** — **KR 전용**, `LIMIT`/`MARKET` 모두 가능, 세션(장전 사전접수) 밖 접수는 원장이 거절할 수 있음.
- `price`: **LIMIT 필수 / MARKET 전달 시 400**. KR 은 정수(원)이고 **호가 단위(tick size) 정합** 필요(틀리면 `invalid-tick-size`/`invalid-request`).
- `quantity`: 기본 **정수만**. **예외: US 시장가 매도(`MARKET`+`SELL`)에 한해 소수점 수량 허용**(소수 6자리까지 — 초과 시 `400 invalid-request`(`fractional-quantity-scale-exceeded`), 정규장 시작~종료 1시간 전에만 접수 — 밖은 `422 fractional-quantity-outside-regular-hours`). 그 외(매수·지정가·KR) 소수점은 `400 invalid-request`, 소수점 매수는 (B) 사용.
- `price`(US): $1 미만 소수 4자리·$1 이상 2자리까지, 초과분은 절삭.

**(B) 금액 기반 (`OrderCreateAmountBased`)** — **US 시장가 전용**:
```json
{ "symbol": "AAPL", "side": "BUY", "orderType": "MARKET", "orderAmount": "100" }
```
- `orderType` **`MARKET` 만**. `orderAmount`(달러) 확정 → 체결 수량 변동(소수점 주식). **정규장 시작~종료 1시간 전에만 접수**(외 시간 `422 amount-order-outside-regular-hours`). (B) 변형엔 `timeInForce` 필드가 스키마에 없음.

**공통 옵션**:
- `clientOrderId`(opt, 최대 36자 `[A-Za-z0-9_-]`): **멱등성 키**. 동일 값 재요청 시 이전 결과 그대로 반환. **10분간 유효**(이후 동일 값은 새 주문). 서버 자동생성 안 함 → **자동매매면 직접 부여 강권**(네트워크 단절 시 중복주문 방지).
- `confirmHighValueOrder`(bool, 기본 false): **1억원 이상** 주문은 `true` 아니면 `400 confirm-high-value-required`. **30억원 이상**은 이 플래그와 무관하게 `422 max-order-amount-exceeded`(overview 에러표엔 없고 필드 설명에만 있음). 동일 `clientOrderId` 로 내용이 다르면 `422 idempotency-key-conflict`.

### OrderModifyRequest
- `orderType`(req `LIMIT|MARKET`), `price`(LIMIT 필수), `confirmHighValueOrder`.
- `quantity`: **KR 필수(양의 정수)** / **US 전달 불가**(주면 `400 us-modify-quantity-not-supported`, US 는 가격 정정만).

### Order (조회 결과)
- orderId, symbol, side, orderType, **timeInForce**(`DAY|CLS|OPG` — OPG=장개시주문, KR 전용), **status**(`OrderStatus`), price?(MARKET 시 null), quantity, orderAmount?(US 금액주문만), currency, orderedAt, canceledAt?, **execution**(`OrderExecution`). 조회는 `OrderStatus` 전 값이 반환될 수 있음.
- `OrderExecution`: filledQuantity, averageFilledPrice?, filledAmount?, commission?, tax?, filledAt?, settlementDate?(결제예정일). (스펙상 키는 항상 존재하고 값만 null 가능 — 필수+nullable.)
- **`OrderStatus` 10종**: `PENDING`(체결대기) · `PENDING_CANCEL` · `PENDING_REPLACE`(정정대기) · `PARTIAL_FILLED` · `FILLED` · `CANCELED` · `REJECTED` · `CANCEL_REJECTED` · `REPLACE_REJECTED` · `REPLACED`.

---

## 6. 에러 코드 전체 표

`code` 기준 분기. HTTP status 와 함께:

| HTTP | code | 의미 |
|---|---|---|
| 400 | `invalid-request` | 호가유형·방향·수량·금액·필수 파라미터 등 잘못된 요청(포괄) |
| 400 | `confirm-high-value-required` | 1억↑ 주문인데 `confirmHighValueOrder!=true` |
| 400 | `account-header-required` | `X-Tossinvest-Account` 헤더 누락 |
| 400 | `unsupported-ranking-duration` | `TOP_GAINERS`·`TOP_LOSERS` 에 `duration=realtime` |
| 400 | `unsupported-symbol` | 시장 지표 카탈로그 밖 심볼(투자자별 매매대금은 KOSPI·KOSDAQ 만) |
| 400 | `unsupported-market` | KR 전용 API(수급 동향)에 타 시장 종목 |
| 400 | `us-modify-quantity-not-supported` | US 정정에 `quantity` 전달 |
| 401 | `invalid-token` | 토큰 무효/형식 오류 |
| 401 | `expired-token` | 토큰 만료 → 재발급 |
| 401 | `token-revoked` | 새 토큰 발급으로 이전 토큰 무효화(client 당 1개) |
| 401 | `edge-blocked` | `Authorization` 헤더 누락(앞단 차단) |
| 401 | `login-user-not-found` | 토큰 대응 로그인 정보 없음 |
| 403 | `forbidden` / `edge-blocked` | 권한 부족 / 비허용 요청 |
| 404 | `stock-not-found` · `exchange-rate-not-found` · `account-not-found` · `order-not-found` · `conditional-order-not-found` · `edge-blocked`(미지원 경로) | 대상 없음 |
| 409 | `request-in-progress` | 동일 `clientOrderId` 생성 요청 처리 중 |
| 409 | `opposite-pending-order-exists` | 동일 종목에 반대 방향 체결 대기 주문 존재(1.1.1 문서엔 422 로 적었으나 overview 는 409) |
| 409 | `already-filled` · `already-canceled` · `already-modified` · `already-rejected` · `already-processing` | 정정/취소 대상 상태 충돌 |
| 422 | `insufficient-buying-power` | 매수가능금액 부족 |
| 422 | `order-hours-closed` | 접수 불가 시간 |
| 422 | `stock-restricted` · `price-out-of-range` · `order-type-not-allowed` · `prerequisite-required`(약관/위험고지 미충족) | 비즈니스 규칙 |
| 422 | `market-not-supported-for-stock`(KR) · `investor-exchange-not-integrated`(KR, SOR 미설정) · `amount-order-outside-regular-hours`(US) · `modify-restricted` · `cancel-restricted` | 시장/주문 제약 |
| 415 | `unsupported-content-type` | 요청 본문은 `application/json` 만(토큰 엔드포인트 제외) |
| 422 | `insufficient-sellable-quantity` · `order-limit-exceeded` · `account-restricted`(RIA·연금 등) · `idempotency-key-conflict` · `duplicate-conditional-order` · `condition-already-met` | 신규 비즈니스 규칙 |
| 422 | `fractional-quantity-outside-regular-hours` · `max-order-amount-exceeded` | overview 표엔 없고 스펙 필드 설명에만 있음 |
| 414 | `edge-blocked` | URI 길이 초과 |
| 429 | `rate-limit-exceeded` / `edge-rate-limit-exceeded` | TPS 초과 → `Retry-After` |
| 500 | `internal-error` / `maintenance` | 일시 장애 / 점검 |

---

## 7. ⚠ 함정 모음 (모르면 당하는 것 한눈에)

*상세는 각 절. 여기는 "이거 몰라서 한 번씩 깨지는" 것만 모음.*

- **토큰 엔드포인트만 응답 포맷이 다르다** — 성공은 OAuth2 표준(`access_token`…, `result` 래핑 안 함), 실패는 `{error, error_description}`. 나머지 전 API 의 `result`/`ErrorResponse` envelope 과 **분기 따로**. (§1·§2)
- **성공 payload 는 `result` 안에 있다** — 최상위가 데이터가 아님. `res.result` 언래핑 안 하면 전부 undefined. (§2)
- **계좌 헤더 누락 = `400 account-header-required`** — 계좌·자산·주문은 전부 `X-Tossinvest-Account` 필수. 시세류만 토큰으로 됨. (§1)
- **`ACCOUNT` 그룹 초당 1회** — `/accounts` 를 매 요청 부르면 즉시 429. accountSeq **한 번 받아 캐싱**. (§3)
- **개장 직후 10분(09:00~09:10) `ORDER_INFO` 반토막**(6→3/s) — 9시 동시호가 전략은 반드시 반영. (`ORDER` 는 2026-09 현재 10/s 유지.) (§3)
- **429 는 헤더가 진실** — `Retry-After` 대기 + 백오프, 한도 숫자는 공지 없이 바뀜(상수 금지). (§3)
- **돈·수량은 전부 문자열 decimal** — `number` 캐스팅 시 반올림으로 금액 틀어짐. Decimal 로 다뤄라. (§2)
- **멱등성은 직접 챙긴다** — `clientOrderId` 줘야만 적용, **10분만 유효**, 서버 자동생성 안 함. 자동매매면 필수(단절 시 중복주문 방지). (§5)
- **고액주문 확인 플래그** — 1억원↑ 은 `confirmHighValueOrder=true` 아니면 `400`, **30억원↑ 은 플래그 무관 `422`**. (§5)
- **US 금액주문·소수점 매도는 정규장(종료 1시간 전까지) + `MARKET` 전용** / **US 정정은 가격만**(quantity 주면 `400 us-modify-quantity-not-supported`). (§4.6·§5)
- **정정·취소는 새 `orderId` 를 반환** — 원주문 id 와 다름. 추적 매핑이 끊기지 않게 연결해둬라. (§4.6)
- **`status=OPEN` 은 `limit`·`cursor` 무시하고 전량 반환**, `CLOSED` 만 페이지네이션(§4.7). (구 문서의 "CLOSED 미지원" 은 해소됨)
- **토큰은 client 당 1개** — 재발급 시 이전 토큰 즉시 무효(`token-revoked`). 다중 프로세스 주의. (§1)
- **허용 IP 미등록 = `403`** (REST·WebSocket 공통). (§0)
- **조건주문 수정은 새 id, 취소는 204 무본문**. (§4.9)
- **KR 지정가 tick size 정합** — 호가 단위 안 맞으면 `400`(예: 5만~20만원 구간 100원 단위). (§5)
- **WebSocket 은 `PING` 텍스트를 60초마다 직접 보내야 한다**(180초 무수신 종료, 서버 데이터는 타이머 리셋 안 함). 계정당 연결 2개 초과 시 오래된 연결이 종료됨. (§4.10)
- **unknown enum/code 관용 구현** — 서버가 값 추가 가능. 모르는 enum/에러코드에 안 깨지게(문서가 명시 요구). (§2)

## 8. 새 프로젝트 설계 체크리스트

- **토큰 매니저**: `expires_in` 만료 전 갱신 + 메모리 캐싱. 401 `expired-token` 시 1회 재발급 후 재시도.
- **accountSeq 캐싱**: 부팅 시 `/accounts` 1회 → 보관(ACCOUNT 1/s).
- **decimal 안전**: 금액/수량은 문자열 그대로 받아 Decimal 로. JS 면 `number` 변환 금지.
- **주문은 항상 `clientOrderId` 부여**(멱등성 10분). 생성 응답 못 받으면 같은 키로 재요청 → 중복 방지.
- **주문 전 사전조회**: `buying-power`/`sellable-quantity` 로 거부 왕복 줄이기. 단 ORDER_INFO 는 피크시간 3/s.
- **레이트리미터**: 그룹별 토큰버킷 클라이언트단 구현 + 429 시 `Retry-After`+백오프+jitter. 9~9:10 ORDER_INFO 반토막 반영.
- **실시간**: 체결·호가·본인 주문은 WebSocket(§4.10) 우선, 재연결 시 `GET /orders` 로 재동기화. 단발 시세는 `/prices` 다건 200개 묶기.
- **장운영 캘린더**로 휴장/세션 판단(하드코딩 금지). KR=KRX+NXT 통합, US=4세션 nullable, **시간은 KST**.
- **unknown enum/code 관용**: 새 값 들어와도 안 깨지게(문서 명시).
- **상태머신**: OrderStatus 10종 + PENDING_*(취소/정정 대기) 전이 처리. 정정/취소는 **새 orderId 반환**(원주문 추적 끊기지 않게 매핑).

---

> 📌 **이 레포에서의 구현 현황**: SDK(`pytossinvest`)는 §1~§5 의 인증·레이트리밋·decimal·에러·엔드포인트를 구현 완료(MIT). 조건주문 5종(§4.9)과 WebSocket(§4.10, `pytossinvest.stream`·`[ws]` extra)은 2026-09-30 래핑 — 미래핑: `/stocks/all`, 수급 동향 5종, `/rankings`, `/market-indicators/*` 3종. MCP 는 조건주문을 2026-09-30 부터 같은 안전모델(가드레일·확인 토큰·등록일 일일한도)로 노출하고, 스트림은 노출하지 않는다(요청/응답 툴 모델과 안 맞음). SDK 의 그룹별 기본 한도(`client.py`)·피크 반토막 대상(`ratelimit.py` `PEAK_GROUPS`)은 1.1.1 수치라 §3 최신값과 어긋난다(보수적 쪽이라 동작엔 무해). MCP 서버(`pytossinvest-mcp`)는 그 위에 안전모델(모드·가드레일·preview→confirm·멱등성)을 얹음(Apache-2.0). 설계·구현 상세는 `docs/superpowers/` 의 spec/plan, 운영 컨벤션은 루트 `AGENTS.md` 참고.
