> **언제 읽나**: `pytossinvest`(SDK) 코드를 만질 때 — 엔드포인트 추가, 모델 변경, 레이트리밋·토큰·에러 매핑 손볼 때. 공개 API 표면과 모듈별 책임·함정의 living 레퍼런스. (외부 토스 API 스펙 자체는 [tossinvest-open-api.md](tossinvest-open-api.md), 설계 시점 기록은 `docs/superpowers/plans/2026-06-17-pytossinvest-sdk.md`.)
>
> **🔄 자가갱신**: SDK 코드를 바꾸면(새 엔드포인트·모델 필드·에러코드·시그니처) **같은 세션에 이 문서를 갱신**한다. 커밋은 수동. 이 문서가 코드와 어긋나면 코드가 진실 — 발견 즉시 고친다.

# pytossinvest (SDK) 내부구조

토스증권 Open API 의 Python 클라이언트. **MIT**. `pytossinvest-mcp` 가 이걸 의존하므로 **공개 API 를 깨면 MCP 가 깨진다** — 시그니처/반환타입 변경 시 MCP 테스트도 그린 확인.

- 위치: `pytossinvest/src/pytossinvest/`
- 테스트: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests` (respx httpx mock + 로컬 websockets 서버, **라이브 키 불필요**)
- 의존: `httpx`(sync), `pydantic` v2. 버전은 `pytossinvest/pyproject.toml` 과 `__init__.__version__`(둘을 같이 올린다. `tests/test_smoke.py` 가 값을 확인한다).

## 레이어 (의존 방향: 위 → 아래)

```
client.py     ← 엔드포인트 메서드 + _request 오케스트레이션 (모든 걸 엮음)
  ├ auth.py        토큰 발급·캐싱·갱신 (httpx I/O)
  ├ ratelimit.py   그룹별 토큰버킷 + 피크 반토막 (pure, 시계 주입)
  ├ errors.py      HTTP/code → 예외 계층 (pure)
  └ models.py      decimal-safe 응답 모델 (pydantic)
money.py      문자열 ↔ Decimal, **float 거부** (pure, 모든 돈 변환의 유일 경로)
```

## 공개 API (`from pytossinvest import ...`)

`__init__.py` 가 export: `TossInvestClient`, 에러 9종(`TossInvestError`·`AuthError`·`ForbiddenError`·`NotFoundError`·`ValidationError`·`ConflictError`·`BusinessRuleError`·`RateLimitError`·`ServerError`·`OAuthError`), 모델 `Account`·`Price`·`BuyingPower`·`OrderResponse`, `to_decimal`·`decimal_to_str`.
- ⚠ `HoldingsItem` 와 `Money` 타입은 `models.__all__` 엔 있지만 **`__init__` 엔 미노출** — 필요하면 `from pytossinvest.models import Money, HoldingsItem`.

### `TossInvestClient`

```python
TossInvestClient(client_id, client_secret, *,
                 base_url="https://openapi.tossinvest.com", timeout=10.0,
                 sleep=time.sleep, monotonic=time.monotonic, now_kst=lambda: datetime.now(KST),
                 max_retries=3, retry_max_wait=60.0, rng=random.random, account_seq=None)
```
주입 가능한 `sleep`/`monotonic`/`now_kst` 는 **테스트 결정성**용(레이트리밋 대기·피크시간 등). `max_retries`/`retry_max_wait`/`rng` 는 **429 bounded 재시도** 제어용(`max_retries=0` 이면 즉시 `RateLimitError`, `retry_max_wait` 는 백오프 상한이자 레이트 게이트 대기 상한, `rng` 는 jitter 테스트 결정성용). `account_seq` 는 계좌 지정용이다. 주면 계좌헤더에 그 값을 쓰고 `get_accounts()` 를 부르지 않으며, 생략하면 `get_accounts()` 의 첫 계좌를 쓴다. 컨텍스트매니저(`with`) 지원, `close()`.

HTTP 연결은 `_HTTP_LIMITS` 로 유휴 60초까지 재사용한다. httpx 기본값은 5초라, 툴콜처럼 간격이 긴 호출이 매번 TCP+TLS 핸드셰이크를 새로 치렀다. 연결 수 상한 두 값은 httpx 클라이언트 기본값을 그대로 적은 것이다(`httpx.Limits()` 만 넘기면 상한이 사라진다).

**메서드** (전부 `_request` 경유):
- 시세(계좌헤더 X): `get_prices(symbols)→Price[]`·`get_orderbook(symbol)`·`get_trades(symbol, count=50)`·`get_candles(symbol, interval, count=100, before=None)`·`get_stocks(symbols)`·`get_exchange_rate(base, quote)`·`get_market_calendar(country, date=None)`·`get_all_stocks(market, *, status=None, security_type=None, common_share=None)`(`STOCK_ALL` 1/s, 시장 전체라 큼)·수급 5종 `get_investor_trading`/`get_program_trades`/`get_short_selling`/`get_credit_trades`/`get_securities_lending(symbol, count=10, until=None)`(`STOCK_TRADING_TREND`, 공통 `_trading_trend`)·`get_rankings(type, market_country, duration, *, exclude_investment_caution=False, count=100)`(`RANKING`)·시장지표 `get_indicator_prices(symbols)`(`MARKET_INDICATOR`)·`get_indicator_candles(symbol, interval, count=100, before=None)`(`MARKET_INDICATOR_CHART`)·`get_index_investor_trading(symbol, interval, count=10, until=None)`(`MARKET_INDICATOR`, KOSPI·KOSDAQ). 선택 파라미터는 `_drop_none` 으로 준 것만 보냄
- 계좌/자산/주문(계좌헤더 O): `get_accounts()→Account[]`(`account_seq` 를 지정하지 않았으면 첫 호출 시 첫 계좌의 `accountSeq` 캐싱)·`get_holdings(symbol=None)`·`get_buying_power(currency)→BuyingPower`·`get_sellable_quantity(symbol)`·`get_commissions()`·`list_orders(status="OPEN", symbol=None, cursor=None, limit=20)`·`get_order(order_id)`
- 주문 write: `place_order(*, symbol, side, order_type, quantity=None, price=None, order_amount=None, time_in_force="DAY", client_order_id=None, confirm_high_value_order=False)→OrderResponse`·`modify_order(order_id, *, order_type, price=None, quantity=None, confirm_high_value_order=False)`·`cancel_order(order_id)`
- 조건주문(계좌헤더 O, `CONDITIONAL_ORDER` 5/s · 조회 `CONDITIONAL_ORDER_HISTORY` 10/s): `create_conditional_order(*, symbol, type, quantity, order_type, expire_date, first, second=None, client_order_id=None, confirm_high_value_order=False)`·`modify_conditional_order(id, *, type, quantity, order_type, expire_date, first, second=None, confirm_high_value_order=False)`(취소+재생성이라 **새 id** 반환)·`cancel_conditional_order(id)→None`(204)·`list_conditional_orders(status="OPEN", symbol=None, cursor=None, limit=20)`·`get_conditional_order(id)`. `first`/`second` 는 **API 형태 dict 그대로**(`orderSide`·`triggerPrice`·`orderPrice?`, 가격 문자열) — 공통 필드는 `_conditional_payload` 한 곳. 전부 dict 반환(얇은 엔드포인트).
- `access_token()→str` — 캐시/갱신된 OAuth 토큰(스트림 핸드셰이크용 공개 접근자).
- **실시간 스트림 `stream.py`** — `TossInvestStream(client, *, url=WS_URL, ping_interval=60)`, async 컨텍스트매니저. `__aenter__` 가 `asyncio.to_thread(client.access_token)` 로 토큰을 받아 `websockets.asyncio.client.connect(..., additional_headers={Authorization})`(websockets 는 **`[ws]` extra, `__aenter__` 안에서만 import** — 기본 설치에 안 끌려옴), keepalive 태스크가 `ping_interval` 마다 텍스트 `PING` 전송(서버는 클라 수신 180초 없으면 끊음). `subscribe(list)` = 구독 전체 교체(`[]` 전체 해제), `async for frame in stream` 은 JSON 프레임 dict 를 내주고 `pong` 은 거른다. 재연결·재구독은 호출자 몫(`personal:order` 는 연결 안에서만 무손실 → 재연결 후 `list_orders` 재동기화). 테스트는 로컬 websockets 서버(`tests/test_stream.py`)로 헤더·구독·PING·프레임 검증 — **실 토스 WS 라이브 검증은 아직 안 함**.
- 진행형 typing: **코어**(accounts/prices/buying-power/orders)는 타입 모델 반환, **얇은** 엔드포인트(holdings/candles/stocks/…)는 언래핑된 `result`(dict/list) 그대로 반환 — 의도된 설계지 TODO 아님.

### `_request` 오케스트레이션 (`client.py`)

1. `_gate(group)` — 그룹 토큰버킷에서 토큰 획득까지 `sleep`. 피크시간(09:00–09:10 KST)엔 `effective_rate` 로 ORDER/ORDER_INFO 버킷 반토막(단, 해당 그룹이 헤더를 한 번이라도 받은 뒤엔 피크반토막 미적용). 토큰 발급(`/oauth2/token`)은 게이트를 타지 않는다.
2. `Authorization: Bearer {token}` (TokenManager). `account=True` 면 `X-Tossinvest-Account: {accountSeq}` — 아직 없으면 **그 자리에서 `get_accounts()` 로 첫 계좌를 캐싱**(계좌가 하나도 없을 때만 `RuntimeError`). 여러 스레드가 동시에 도착해도 조회는 한 번이다(`_account_lock`). 2026-09-30 전엔 `get_accounts()` 선호출을 `RuntimeError` 로 강제했는데, mcp 2.1+ 가 예외 텍스트를 가려 MCP 의 계좌 툴이 이유 없이 실패했다.
3. **응답 직후(상태코드 분기 전) → 응답 헤더로 해당 그룹 버킷 동기화(`_sync_bucket_from_headers`)**. 이어 200 이면 `resp.json()["result"]` 언래핑 반환(본문이 JSON 이 아니면 `invalid-response`, JSON 객체가 아니거나 `result` 가 없으면 `missing-result`), **204 는 `None`**(조건주문 DELETE — 본문도 envelope 도 없음. 이전엔 비2xx 로 떨어져 에러가 됐을 경로).
4. **401 + `code` ∈ `_REISSUE_TOKEN_CODES`(`expired-token`·`token-revoked`) → `token.invalidate(token)` 후 1회 재시도**(429 카운터 보존). 넘긴 `token` 은 이 요청이 실어 보낸 토큰이다. `token-revoked` 는 토스가 client 당 토큰 1개만 살려 두기 때문 — 같은 키로 다른 프로세스가 발급하면 이쪽 토큰이 죽는다(두 프로세스가 번갈아 쓰면 호출마다 재발급 핑퐁, `AUTH` 5/s).
5. **429 + 잔여 시도 있음 → `backoff_wait(attempt, retry_after)` 만큼 sleep 후 같은 요청 재시도**(`_attempt` 파라미터로 횟수 추적). 소진 시 `RateLimitError` 던짐. **5xx·타임아웃은 재시도 안 함**.
6. 그 외 비2xx → `error_from_response(status, body, headers)` 던짐.

### 스레드 공유

클라이언트 하나를 여러 스레드가 동시에 써도 된다. `pytossinvest-mcp` 가 쓰는 mcp SDK 는 sync 툴을 워커 스레드에서 실행하므로 병렬 툴콜이 곧 동시 호출이고, `TossInvestStream` 도 `asyncio.to_thread(client.access_token)` 으로 다른 스레드에서 토큰을 읽는다. 락은 세 개다.

- `TokenManager._lock`: 캐시 확인부터 발급 HTTP 왕복까지 잡는다. 토스는 client 당 유효 토큰이 1개라, 두 스레드가 동시에 발급하면 서로의 토큰을 죽인다. 기다린 스레드는 먼저 받은 토큰을 그대로 쓴다. `invalidate(token)` 은 실패한 토큰이 아직 캐시된 토큰일 때만 버린다. 늦게 도착한 401 이 다른 스레드가 방금 재발급한 토큰을 버리면 재발급이 연쇄되기 때문이다.
- `TossInvestClient._account_lock`: 첫 `get_accounts()` 조회를 한 번으로 묶는다. `ACCOUNT` 는 1/s 라 조회가 겹치면 초 단위로 줄을 선다.
- `TossInvestClient._rate_lock`: 버킷과 `_rate_from_header` 를 지킨다. `TokenBucket` 자체에는 락이 없다. 게이트의 `sleep` 은 락 밖에서 한다.

클라이언트에 가변 상태를 더하면 같은 방식으로 락을 건다. 같은 키를 쓰는 **다른 프로세스**가 토큰을 발급해 생기는 revoke 는 SDK 가 막지 못하고, 4번의 401 재시도로 복구한다.

### 경로에 들어가는 값

주문 id·조건주문 id·심볼·국가코드는 `_path_seg()` 를 거쳐 경로에 들어간다. 호출자가 준 값(MCP 뒤에서는 LLM 이 채운 값)이 다른 엔드포인트를 고르지 못하게 하려는 것이다.

- `/`, `?`, `#` 등은 퍼센트 인코딩되어 값이 경로 세그먼트 하나로 남는다. `BRK.B` 같은 평범한 값은 그대로 나간다.
- `""`, `.`, `..` 는 요청을 보내기 전에 `ValueError` 로 거부한다. 인코딩으로는 막을 수 없는 값이다(`.`·`..` 은 httpx 가 경로 이동으로 풀고, 빈 값은 세그먼트를 없앤다).
- 서버가 `%2F` 를 디코딩한 뒤 경로를 다시 정규화하는지는 클라이언트가 통제할 수 없다.

## 모듈별 핵심 + 함정

- **`money.py`**: `to_decimal(str|int|Decimal)→Decimal`. **`bool`·`float` 은 `TypeError`** (float 진입 경로 자체를 안 만듦). `NaN`·`Infinity` 는 파싱 불가 문자열과 똑같이 `decimal.InvalidOperation`(음수는 허용, 실현손실 등). `decimal_to_str` = `format(v, "f")`(지수표기 방지). 돈/수량은 무조건 이걸 통과.
- **`models.py`**: `Money = Annotated[Decimal, BeforeValidator(to_decimal)]` — pydantic 이 문자열→Decimal 강제하고 float 거부. `_Base` 는 `populate_by_name=True, extra="ignore"`(서버가 필드 추가해도 안 깨짐). 모델: `Account`(account_no/account_seq/account_type)·`Price`(last_price:Money)·`BuyingPower`(cash_buying_power:Money)·`OrderResponse`(order_id/client_order_id?)·`HoldingsItem`.
- **`errors.py`**: `error_from_response` 가 **status→예외클래스** 매핑(400 Validation·401 Auth·403 Forbidden·404 NotFound·409 Conflict·422 BusinessRule·429 RateLimit). **모르는 status 는 ≥500 → ServerError, 그 외 → base `TossInvestError`**(안 깨짐). `code` 는 항상 문자열이고 기본값 `"unknown"`(문자열이 아닌 값이 오면 `"unknown"` — 호출자가 `code in {...}` 로 분기하므로). 본문이 표준 모양이 아니어도(JSON 문자열·배열, `error` 가 문자열인 게이트웨이 응답) status 로 고른 클래스에 `code="unknown"` 으로 떨어지고 `AttributeError` 를 내지 않는다. 토큰 엔드포인트만 `oauth_error_from_response`(OAuth2 포맷). `RateLimitError.retry_after` 는 429 의 `Retry-After` 헤더에서만 파싱.
- **`ratelimit.py`**: `TokenBucket(capacity, refill_per_sec, now)` — `try_acquire(n=1)`/`time_until_available(n=1)`. `effective_rate(group, base, now_kst)` 가 `PEAK_GROUPS={ORDER, ORDER_INFO}` 를 09:00–09:10 KST 동안 반토막. **v0.0.2 구현**: `backoff_wait(attempt, retry_after, *, base=1.0, cap, rng)` 헬퍼(`Retry-After>0` 이면 그대로, 아니면 지수백오프+full jitter, `cap` 상한); `_sync_bucket_from_headers(group, headers)` 가 `X-RateLimit-Limit`→capacity, `1/X-RateLimit-Reset`→refill_per_sec, `min(_tokens, X-RateLimit-Remaining)`→_tokens 로 버킷 동기화, 헤더 없거나 파싱 불가면 no-op. 토큰을 영영 내주지 못할 값(비유한, `Limit<1`, 음수)도 무시한다(받아들이면 게이트가 무한 루프나 0 나눗셈에 빠진다). 재충전 주기는 `retry_max_wait` 로 잘라서, 게이트 대기가 그보다 길어질 상황이면 그냥 보내고 유계인 429 재시도 경로에 맡긴다; 그룹별 `_rate_from_header` 플래그 — 헤더를 한 번 받은 그룹은 `_gate` 에서 피크반토막 미적용(헤더가 진실).
- **`auth.py`**: `TokenManager.get_token()` 이 `expires_in - 30s` 버퍼까지 메모리 캐싱. `invalidate(token=None)` 로 강제 재발급(client 의 401 재시도가 실패한 토큰을 넘겨 호출, 인자 없이 부르면 무조건 버림). `_fetch` 는 form-urlencoded, 비200 이면 `OAuthError`, 200 인데 본문이 깨졌으면(`access_token`·`expires_in` 누락, 비-JSON) `OAuthError("invalid-response")`. 락 동작은 위 '스레드 공유'.

## 새 엔드포인트 추가 절차

1. (코어면) `models.py` 에 decimal-safe 모델 추가(돈 필드는 `Money`).
2. `client.py` 에 메서드 추가 — `self._request(method, path, group=..., account=True/False, params/json=...)`. 그룹은 `_GROUP_RATES` 키(없으면 추가). 계좌컨텍스트면 `account=True`. 경로에 호출자 값을 넣을 땐 `_path_seg()` 로 감싼다.
3. 코어면 모델 검증, 얇으면 `result` 그대로 반환.
4. 필요 시 `__init__.py` export 추가.
5. respx mock 테스트(fixture 응답) — 라이브 키 없이. **TDD**: 실패 테스트 먼저.
6. 이 문서 + (외부 스펙 변경이면) [tossinvest-open-api.md](tossinvest-open-api.md) 갱신.

## 이력

- 2026-11-12 스레드 공유 지원(토큰 발급을 락으로 한 번에 묶음, `invalidate(token)`, 계좌 조회·버킷 락). 그전엔 MCP 병렬 툴콜 4개 중 3개가 `token-revoked` 로 실패했다. 경로 값 인코딩(`_path_seg`), 비표준 에러 본문·토큰 응답 처리, 레이트리밋 헤더 검증과 게이트 대기 상한, `account_seq` 옵션, `to_decimal` 비유한값 거부 추가. keepalive 60초(실측: 새 연결 53ms 중 핸드셰이크 39ms, 재사용 약 15ms, 서버는 유휴 130초까지 연결 유지). 쓰이지 않던 `AUTH` 버킷과 `_request(data=)` 제거.
