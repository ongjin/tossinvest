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
