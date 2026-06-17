# pytossinvest

**토스증권 Open API 의 비공식(unofficial) Python SDK.** 돈은 절대 `float` 가 아니고, 레이트리밋·멱등성·토큰 생애주기를 손으로 제대로 다룬 동기(sync) 클라이언트.

![python](https://img.shields.io/badge/python-3.12+-3776ab)
![license](https://img.shields.io/badge/license-MIT-3da639)
![tests](https://img.shields.io/badge/tests-46%20passing-2ea44f)
![status](https://img.shields.io/badge/Toss%20API-pre--launch-f0ad4e)
![unofficial](https://img.shields.io/badge/unofficial-%E2%9A%A0-9e9e9e)

> ⚠️ **비공식 클라이언트** — 토스증권과 무관하며 상표/엔도르스먼트와도 무관합니다. 토스 Open API 는 2026-06 기준 **사전신청 단계**라, 이 SDK 는 라이브 API 가 아니라 **응답 fixture(`respx` mock)** 에 대해 개발·테스트됩니다. 정식 오픈 시 일부 동작이 바뀔 수 있습니다.

> 🤖 AI 에게 계좌를 *안전하게* 쥐여주는 MCP 서버를 찾는다면 → [`tossinvest-mcp`](../tossinvest-mcp/) (이 SDK 위에 안전모델을 얹은 패키지).

---

## 무엇을 해주나

자동생성 래퍼는 토스 API 의 *함정*을 안 막아줍니다. 그래서 손으로, 제대로:

| | |
|---|---|
| 💸 **돈은 절대 float 가 아니다** | 금액·수량 전구간 문자열/`Decimal`. `float` 은 들어오는 순간 `TypeError` — 부동소수 반올림으로 1원도 안 틀어집니다. |
| 🚦 **클라이언트단 레이트리미터** | 10개 그룹별 토큰버킷이 요청 속도를 조절. **09:00–09:10 KST 개장 동시호가 10분간 ORDER/ORDER_INFO 반토막**(6→3) 반영. |
| 🔁 **멱등성** | `clientOrderId` 로 중복주문 방지 — 네트워크 단절로 응답을 못 받아도 같은 키로 재시도하면 두 번 체결되지 않음(서버측 ~10분 유효). |
| 🧩 **에러는 `code` 로 분기** | `message` 가 비어도 OK. 서버가 **모르는 code/enum 을 추가해도 안 깨짐**(관용적 파싱). |
| 🔐 **토큰 생애주기** | 만료 30초 전까지 메모리 캐싱·자동 갱신(`expires_in` 이 30초 이하라도 과거 시각으로 뭉개지지 않게 `max(0, …)` 클램프), `401 expired-token` 시 1회 재발급 후 재시도. |
| ✅ **라이브 키 없이 그린** | `pytest` → **46개 테스트** 통과(respx mock, 네트워크 0). 기여 장벽 0. |

---

## 설치

```bash
# PyPI (정식 오픈 후)
pip install pytossinvest

# 소스에서 (uv 워크스페이스 모노레포)
git clone <repo> && cd toss
uv sync --package pytossinvest --extra dev
```

요구사항: **Python 3.12+**. 의존성은 `httpx`(sync) + `pydantic` v2 뿐.

---

## 빠른 시작

```python
from pytossinvest import TossInvestClient

with TossInvestClient(client_id="...", client_secret="...") as c:
    # 1) 계좌 — 첫 호출 시 accountSeq 를 자동 캐싱 (ACCOUNT 그룹 1 req/s)
    accounts = c.get_accounts()

    # 2) 시세 — 계좌 컨텍스트 불필요
    prices = c.get_prices(["005930"])
    print(prices[0].last_price)          # Decimal("70000") — 절대 float 아님

    # 3) 주문 — 문자열-decimal + 멱등(clientOrderId 직접 부여)
    resp = c.place_order(
        symbol="005930", side="BUY", order_type="LIMIT",
        price="70000", quantity="10", client_order_id="my-001",
    )
    print(resp.order_id)
```

`with` 블록을 벗어나면 내부 `httpx.Client` 가 닫힙니다(`c.close()` 수동 호출도 가능).

> **돈/수량은 무조건 문자열로 넣고 `Decimal` 로 받습니다.** `price=70000.0` 처럼 float 을 넣으면 `TypeError`. 의도된 안전장치입니다.

---

## 공개 API 표면

`from pytossinvest import ...` 로 노출되는 것 전부:

```python
from pytossinvest import (
    TossInvestClient,
    # 에러 (10종, 전부 TossInvestError 상속)
    TossInvestError, AuthError, ForbiddenError, NotFoundError,
    ValidationError, ConflictError, BusinessRuleError,
    RateLimitError, ServerError, OAuthError,
    # 모델
    Account, Price, BuyingPower, OrderResponse,
    # money 헬퍼
    to_decimal, decimal_to_str,
)
```

> `Money` 타입 별칭과 `HoldingsItem` 모델은 `__init__` 엔 미노출입니다. 필요하면 `from pytossinvest.models import Money, HoldingsItem`.

### `TossInvestClient`

```python
TossInvestClient(
    client_id, client_secret, *,
    base_url="https://openapi.tossinvest.com",
    timeout=10.0,
    sleep=time.sleep,          # 주입 가능 (레이트리밋 대기) — 테스트 결정성용
    monotonic=time.monotonic,  # 주입 가능 (토큰버킷·토큰만료)
    now_kst=lambda: datetime.now(KST),  # 주입 가능 (피크시간 판정)
)
```

`sleep`/`monotonic`/`now_kst` 주입은 **테스트 결정성**을 위한 것입니다(가짜 시계로 레이트리밋·피크시간을 제어). 일반 사용 시엔 신경 쓸 필요 없습니다.

### 메서드

모든 메서드는 내부 `_request` 오케스트레이션을 거칩니다(레이트게이트 → 인증 → 계좌헤더 → 언래핑 → 401 재시도).

**시세 (계좌 컨텍스트 불필요)**

| 메서드 | 반환 | 그룹 |
|---|---|---|
| `get_prices(symbols: list[str])` | `list[Price]` | `MARKET_DATA` |
| `get_orderbook(symbol)` | `dict` | `MARKET_DATA` |
| `get_trades(symbol, count=50)` | `list` | `MARKET_DATA` |
| `get_candles(symbol, interval, count=100, before=None)` | `dict` | `MARKET_DATA_CHART` |
| `get_stocks(symbols: list[str])` | `list` | `STOCK` |
| `get_exchange_rate(base, quote)` | `dict` | `MARKET_INFO` |
| `get_market_calendar(country, date=None)` | `dict` | `MARKET_INFO` |

**계좌 / 자산 / 주문 (계좌 헤더 `X-Tossinvest-Account` 자동 부착)**

| 메서드 | 반환 | 그룹 |
|---|---|---|
| `get_accounts()` | `list[Account]` · **첫 호출 시 `accountSeq` 캐싱** | `ACCOUNT` |
| `get_holdings(symbol=None)` | `dict` | `ASSET` |
| `get_buying_power(currency)` | `BuyingPower` | `ORDER_INFO` |
| `get_sellable_quantity(symbol)` | `dict` | `ORDER_INFO` |
| `get_commissions()` | `list` | `ORDER_INFO` |
| `list_orders(status="OPEN", symbol=None, cursor=None, limit=20)` | `dict` | `ORDER_HISTORY` |
| `get_order(order_id)` | `dict` | `ORDER_HISTORY` |

**주문 write (계좌 헤더)**

```python
place_order(*, symbol, side, order_type,
            quantity=None, price=None, order_amount=None,
            time_in_force="DAY", client_order_id=None,
            confirm_high_value_order=False) -> OrderResponse

modify_order(order_id, *, order_type,
             price=None, quantity=None,
             confirm_high_value_order=False) -> dict

cancel_order(order_id) -> dict
```

> **타입화 설계**: 코어 엔드포인트(accounts/prices/buying-power/orders)는 검증된 **pydantic 모델**을 반환하고, 얇은 엔드포인트(holdings/candles/stocks/orderbook/trades/…)는 **언래핑된 `result`**(dict/list)를 그대로 반환합니다. TODO 가 아니라 의도된 절충입니다 — 변동이 잦은 응답은 강타입을 강요하지 않습니다.

---

## 핵심 동작

### `_request` 오케스트레이션

1. **레이트 게이트** — 해당 그룹 토큰버킷에서 토큰을 얻을 때까지 `sleep`. 피크시간(09:00–09:10 KST)엔 ORDER/ORDER_INFO 버킷을 반토막.
2. **인증** — `Authorization: Bearer {token}` (TokenManager 가 캐싱·갱신).
3. **계좌 헤더** — `account=True` 엔드포인트는 `X-Tossinvest-Account: {accountSeq}` 부착. `accountSeq` 가 없으면 `RuntimeError` → **`get_accounts()` 를 먼저 호출**해야 합니다.
4. **언래핑** — `200` 이면 `resp.json()["result"]` 를 반환(토큰 엔드포인트 제외). 바디가 **비-JSON·과도하게 중첩된 JSON(`RecursionError`)이거나 `result` 키가 없으면** `TossInvestError`(`invalid-response` / `missing-result`)로 거부 — `None` 을 조용히 순회하다 `TypeError` 로 깨지지 않게.
5. **401 재시도** — `code == "expired-token"` 이면 토큰을 무효화하고 **1회** 재발급 후 재시도.
6. 그 외 비2xx → `code` 기반 예외로 변환해 raise.

### 돈 / Decimal 규약 (`money.py`)

```python
to_decimal("70000")        # Decimal("70000")
to_decimal(70000)          # Decimal("70000")   — int 허용
to_decimal(70000.0)        # TypeError!         — float 금지
to_decimal(True)           # TypeError!         — bool 금지
decimal_to_str(Decimal("70000.50"))  # "70000.50"  — 지수표기 방지(format(v,"f"))
```

`Money = Annotated[Decimal, BeforeValidator(to_decimal)]` 로 pydantic 모델도 문자열→Decimal 을 강제하고 float 을 거부합니다. 모든 돈/수량은 이 한 경로를 통과합니다.

### 레이트리밋 (`ratelimit.py`)

그룹별 기본 TPS(토큰버킷 capacity = refill/sec):

| 그룹 | TPS | 그룹 | TPS |
|---|---|---|---|
| `AUTH` | 5 | `MARKET_DATA` | 10 |
| `ACCOUNT` | 1 | `MARKET_DATA_CHART` | 5 |
| `ASSET` | 5 | `ORDER` | 6 |
| `STOCK` | 5 | `ORDER_HISTORY` | 5 |
| `MARKET_INFO` | 3 | `ORDER_INFO` | 6 |

**피크 반토막**: `PEAK_GROUPS = {ORDER, ORDER_INFO}` 는 09:00–09:10 KST 개장 동시호가 동안 TPS 가 절반(6→3)으로 떨어집니다. 버킷이 요청 속도를 조절(pacing)하지만, 서버가 그래도 `429` 를 주면 `RateLimitError` 로 표면화됩니다.

### 에러 처리 (`errors.py`)

HTTP status → 예외 클래스 매핑:

| status | 예외 | status | 예외 |
|---|---|---|---|
| 400 | `ValidationError` | 422 | `BusinessRuleError` |
| 401 | `AuthError` | 429 | `RateLimitError` |
| 403 | `ForbiddenError` | ≥500 | `ServerError` |
| 404 | `NotFoundError` | 그 외 | `TossInvestError`(base) |
| 409 | `ConflictError` | (토큰 EP) | `OAuthError` |

모든 예외는 `.code`(기본 `"unknown"`), `.message`, `.http_status`, `.request_id`, `.data`, `.retry_after` 를 가집니다. **`code` 로 분기**하면 서버가 모르는 코드를 추가해도 안 깨집니다. `retry_after` 는 `429` 의 `Retry-After` 헤더에서만 파싱됩니다.

```python
from pytossinvest import RateLimitError, BusinessRuleError

try:
    c.place_order(...)
except RateLimitError as e:
    time.sleep(e.retry_after or 1.0)   # 재시도 정책은 호출자 책임 (아래 한계 참고)
except BusinessRuleError as e:
    if e.code == "insufficient-buying-power":
        ...
```

### 멱등성

`place_order(client_order_id="my-001")` — 같은 `clientOrderId` 로 재시도하면 서버가 중복을 막아 **두 번 체결되지 않습니다**(서버측 ~10분 유효). `clientOrderId` 는 **자동 생성하지 않으니** 호출자가 직접 부여해야 합니다.

---

## v0.0.1 한계

- **레이트리밋은 정적 기본값 + 피크 반토막만** — 응답의 `X-RateLimit-*` 헤더로 동적 동기화하는 기능은 **미구현**(v0.0.2 예정). 버킷이 pacing 은 하지만 헤더가 진실인 상황은 아직 못 따라갑니다.
- **자동 retry/backoff 없음** — SDK 는 `RateLimitError`/`AuthError` 를 던질 뿐, 재시도 오케스트레이션은 **호출자**(또는 [`tossinvest-mcp`](../tossinvest-mcp/) 레이어) 책임입니다.
- **`clientOrderId` 자동 생성 안 함** — 멱등성을 원하면 직접 부여하세요.

---

## 테스트

```bash
uv run --package pytossinvest --extra dev pytest pytossinvest/tests   # 46 passing
```

`respx` 로 httpx 를 mock 합니다 — **라이브 키 불필요, 네트워크 0**. `git clone && uv sync && pytest` 면 그린.

---

## 라이선스

**MIT** — 패키지 디렉터리의 [`LICENSE`](LICENSE) 참고. (상위 MCP 서버 [`tossinvest-mcp`](../tossinvest-mcp/) 는 Apache-2.0.)
