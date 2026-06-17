# pytossinvest

Unofficial Python client for the Toss Securities Open API. MIT licensed.

## Status

The Toss Securities Open API is currently in pre-launch. This SDK is developed and tested against response fixtures rather than the live API, so behaviour may change once the API is publicly available.

## Usage

```python
from pytossinvest import TossInvestClient

with TossInvestClient(client_id="...", client_secret="...") as c:
    c.get_accounts()                 # caches accountSeq
    prices = c.get_prices(["005930"])
    print(prices[0].last_price)      # Decimal

    # Orders are string-decimal and idempotent (pass your own clientOrderId)
    c.place_order(symbol="005930", side="BUY", order_type="LIMIT",
                  price="70000", quantity="10", client_order_id="my-001")
```

> Money and quantities are always `Decimal` / strings — never floats.
> The SDK is tested against fixtures; live calls require Toss Open API credentials.

## Limitations (v0.0.1)

- **Rate limiting uses static documented defaults + peak-hour halving** (ORDER/ORDER_INFO are throttled 6→3 req/s during the 09:00–09:10 KST opening-auction window). Dynamic `X-RateLimit-*` response-header sync is **not yet implemented** (planned for v0.0.2). The per-group token bucket paces requests; if the server still returns `429`, it surfaces as `RateLimitError` with `.retry_after` for the caller to honor.
- **Automatic retry/backoff is not built in.** The SDK raises `RateLimitError`/`AuthError`; retry orchestration is left to the caller (or the upcoming MCP layer).
- `clientOrderId` is **not auto-generated** — pass your own for idempotency (it is valid for ~10 minutes server-side).
