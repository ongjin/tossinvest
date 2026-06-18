> **언제 읽나**: `pytossinvest`(SDK) 코드를 만질 때 — 엔드포인트 추가, 모델 변경, 레이트리밋·토큰·에러 매핑 손볼 때. 공개 API 표면과 모듈별 책임·함정의 living 레퍼런스. (외부 토스 API 스펙 자체는 [tossinvest-open-api.md](tossinvest-open-api.md), 설계 시점 기록은 `docs/superpowers/plans/2026-06-17-pytossinvest-sdk.md`.)
>
> **🔄 자가갱신**: SDK 코드를 바꾸면(새 엔드포인트·모델 필드·에러코드·시그니처) **같은 세션에 이 문서를 갱신**한다. 커밋은 수동. 이 문서가 코드와 어긋나면 코드가 진실 — 발견 즉시 고친다.

# pytossinvest (SDK) 내부구조

토스증권 Open API 의 Python 클라이언트. **MIT**. `tossinvest-mcp` 가 이걸 의존하므로 **공개 API 를 깨면 MCP 가 깨진다** — 시그니처/반환타입 변경 시 MCP 테스트도 그린 확인.

- 위치: `pytossinvest/src/pytossinvest/`
- 테스트: `uv run --package pytossinvest --extra dev pytest pytossinvest/tests` (respx httpx mock, 59개, **라이브 키 불필요**)
- 의존: `httpx`(sync), `pydantic` v2. 버전 v0.0.2.

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
                 max_retries=3, retry_max_wait=60.0, rng=random.random)
```
주입 가능한 `sleep`/`monotonic`/`now_kst` 는 **테스트 결정성**용(레이트리밋 대기·피크시간 등). `max_retries`/`retry_max_wait`/`rng` 는 **429 bounded 재시도** 제어용(`max_retries=0` 이면 즉시 `RateLimitError`, `retry_max_wait` 는 백오프 상한, `rng` 는 jitter 테스트 결정성용). 컨텍스트매니저(`with`) 지원, `close()`.

**메서드** (전부 `_request` 경유):
- 시세(계좌헤더 X): `get_prices(symbols)→Price[]`·`get_orderbook(symbol)`·`get_trades(symbol, count=50)`·`get_candles(symbol, interval, count=100, before=None)`·`get_stocks(symbols)`·`get_exchange_rate(base, quote)`·`get_market_calendar(country, date=None)`
- 계좌/자산/주문(계좌헤더 O): `get_accounts()→Account[]`(첫 호출 시 `accountSeq` 자동 캐싱)·`get_holdings(symbol=None)`·`get_buying_power(currency)→BuyingPower`·`get_sellable_quantity(symbol)`·`get_commissions()`·`list_orders(status="OPEN", symbol=None, cursor=None, limit=20)`·`get_order(order_id)`
- 주문 write: `place_order(*, symbol, side, order_type, quantity=None, price=None, order_amount=None, time_in_force="DAY", client_order_id=None, confirm_high_value_order=False)→OrderResponse`·`modify_order(order_id, *, order_type, price=None, quantity=None, confirm_high_value_order=False)`·`cancel_order(order_id)`
- 진행형 typing: **코어**(accounts/prices/buying-power/orders)는 타입 모델 반환, **얇은** 엔드포인트(holdings/candles/stocks/…)는 언래핑된 `result`(dict/list) 그대로 반환 — 의도된 설계지 TODO 아님.

### `_request` 오케스트레이션 (`client.py`)

1. `_gate(group)` — 그룹 토큰버킷에서 토큰 획득까지 `sleep`. 피크시간(09:00–09:10 KST)엔 `effective_rate` 로 ORDER/ORDER_INFO 버킷 반토막(단, 해당 그룹이 헤더를 한 번이라도 받은 뒤엔 피크반토막 미적용).
2. `Authorization: Bearer {token}` (TokenManager). `account=True` 면 `X-Tossinvest-Account: {accountSeq}` (없으면 RuntimeError → `get_accounts()` 먼저 호출 강제).
3. **응답 직후(상태코드 분기 전) → 응답 헤더로 해당 그룹 버킷 동기화(`_sync_bucket_from_headers`)**. 이어 200 이면 `resp.json()["result"]` 언래핑 반환.
4. **401 + `code=="expired-token"` → `token.invalidate()` 후 1회 재시도**(429 카운터 보존).
5. **429 + 잔여 시도 있음 → `backoff_wait(attempt, retry_after)` 만큼 sleep 후 같은 요청 재시도**(`_attempt` 파라미터로 횟수 추적). 소진 시 `RateLimitError` 던짐. **5xx·타임아웃은 재시도 안 함**.
6. 그 외 비2xx → `error_from_response(status, body, headers)` 던짐.

## 모듈별 핵심 + 함정

- **`money.py`**: `to_decimal(str|int|Decimal)→Decimal`. **`bool`·`float` 은 `TypeError`** (float 진입 경로 자체를 안 만듦). `decimal_to_str` = `format(v, "f")`(지수표기 방지). 돈/수량은 무조건 이걸 통과.
- **`models.py`**: `Money = Annotated[Decimal, BeforeValidator(to_decimal)]` — pydantic 이 문자열→Decimal 강제하고 float 거부. `_Base` 는 `populate_by_name=True, extra="ignore"`(서버가 필드 추가해도 안 깨짐). 모델: `Account`(account_no/account_seq/account_type)·`Price`(last_price:Money)·`BuyingPower`(cash_buying_power:Money)·`OrderResponse`(order_id/client_order_id?)·`HoldingsItem`.
- **`errors.py`**: `error_from_response` 가 **status→예외클래스** 매핑(400 Validation·401 Auth·403 Forbidden·404 NotFound·409 Conflict·422 BusinessRule·429 RateLimit). **모르는 status 는 ≥500 → ServerError, 그 외 → base `TossInvestError`**(안 깨짐). `code` 기본값 `"unknown"`. 토큰 엔드포인트만 `oauth_error_from_response`(OAuth2 포맷). `RateLimitError.retry_after` 는 429 의 `Retry-After` 헤더에서만 파싱.
- **`ratelimit.py`**: `TokenBucket(capacity, refill_per_sec, now)` — `try_acquire(n=1)`/`time_until_available(n=1)`. `effective_rate(group, base, now_kst)` 가 `PEAK_GROUPS={ORDER, ORDER_INFO}` 를 09:00–09:10 KST 동안 반토막. **v0.0.2 구현**: `backoff_wait(attempt, retry_after, *, base=1.0, cap, rng)` 헬퍼(`Retry-After>0` 이면 그대로, 아니면 지수백오프+full jitter, `cap` 상한); `_sync_bucket_from_headers(group, headers)` 가 `X-RateLimit-Limit`→capacity, `1/X-RateLimit-Reset`→refill_per_sec, `min(_tokens, X-RateLimit-Remaining)`→_tokens 로 버킷 동기화, 헤더 없거나 파싱 불가면 no-op; 그룹별 `_rate_from_header` 플래그 — 헤더를 한 번 받은 그룹은 `_gate` 에서 피크반토막 미적용(헤더가 진실).
- **`auth.py`**: `TokenManager.get_token()` 이 `expires_in - 30s` 버퍼까지 메모리 캐싱. `invalidate()` 로 강제 재발급(client 의 401 재시도가 호출). `_fetch` 는 form-urlencoded, 비200 이면 `OAuthError`.

## 새 엔드포인트 추가 절차

1. (코어면) `models.py` 에 decimal-safe 모델 추가(돈 필드는 `Money`).
2. `client.py` 에 메서드 추가 — `self._request(method, path, group=..., account=True/False, params/json=...)`. 그룹은 `_GROUP_RATES` 키(없으면 추가). 계좌컨텍스트면 `account=True`.
3. 코어면 모델 검증, 얇으면 `result` 그대로 반환.
4. 필요 시 `__init__.py` export 추가.
5. respx mock 테스트(fixture 응답) — 라이브 키 없이. **TDD**: 실패 테스트 먼저.
6. 이 문서 + (외부 스펙 변경이면) [tossinvest-open-api.md](tossinvest-open-api.md) 갱신.
