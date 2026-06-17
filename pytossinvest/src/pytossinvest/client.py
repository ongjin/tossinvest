from __future__ import annotations

import time as _time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from .auth import TokenManager
from .errors import error_from_response
from .ratelimit import TokenBucket, effective_rate

__all__ = ["TossInvestClient"]

_KST = ZoneInfo("Asia/Seoul")

# Base TPS per group. v0.0.1 uses these static documented defaults plus peak-hour
# halving (applied in _gate). Dynamic X-RateLimit-* header sync is not yet implemented
# (tracked for v0.0.2); the bucket paces requests and 429s surface as RateLimitError.
_GROUP_RATES: dict[str, float] = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "ORDER": 6,
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,
}


class TossInvestClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = "https://openapi.tossinvest.com",
        timeout: float = 10.0,
        sleep: Callable[[float], None] = _time.sleep,
        monotonic: Callable[[], float] = _time.monotonic,
        now_kst: Callable[[], datetime] = lambda: datetime.now(_KST),
    ):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._token = TokenManager(client_id, client_secret, http=self._http, now=monotonic)
        self._sleep = sleep
        self._now_kst = now_kst
        self._buckets: dict[str, TokenBucket] = {
            g: TokenBucket(capacity=r, refill_per_sec=r, now=monotonic)
            for g, r in _GROUP_RATES.items()
        }
        self._account_seq: int | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TossInvestClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _gate(self, group: str) -> None:
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        # Apply peak-hour halving (09:00-09:10 KST for ORDER/ORDER_INFO).
        rate = effective_rate(group, _GROUP_RATES[group], self._now_kst())
        bucket.capacity = rate
        bucket.refill_per_sec = rate
        while not bucket.try_acquire():
            self._sleep(bucket.time_until_available())

    def _request(
        self,
        method: str,
        path: str,
        *,
        group: str,
        account: bool = False,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
        _retried: bool = False,
    ) -> Any:
        self._gate(group)
        headers = {"Authorization": f"Bearer {self._token.get_token()}"}
        if account:
            if self._account_seq is None:
                raise RuntimeError("account context required but accountSeq not cached; call get_accounts() first")
            headers["X-Tossinvest-Account"] = str(self._account_seq)

        resp = self._http.request(
            method, path, params=params, json=json, data=data, headers=headers
        )

        if resp.status_code == 200:
            return resp.json().get("result")

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if (
            resp.status_code == 401
            and not _retried
            and (body.get("error") or {}).get("code") == "expired-token"
        ):
            self._token.invalidate()
            return self._request(
                method, path, group=group, account=account,
                params=params, json=json, data=data, _retried=True,
            )

        raise error_from_response(resp.status_code, body, resp.headers)

    # --- account / asset ---
    def get_accounts(self) -> list:
        from .models import Account
        result = self._request("GET", "/api/v1/accounts", group="ACCOUNT")
        accounts = [Account.model_validate(a) for a in result]
        if self._account_seq is None and accounts:
            self._account_seq = accounts[0].account_seq
        return accounts

    def get_holdings(self, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/api/v1/holdings", group="ASSET", account=True, params=params)

    # --- market data ---
    def get_prices(self, symbols: list[str]) -> list:
        from .models import Price
        result = self._request(
            "GET", "/api/v1/prices", group="MARKET_DATA",
            params={"symbols": ",".join(symbols)},
        )
        return [Price.model_validate(p) for p in result]

    def get_orderbook(self, symbol: str) -> dict:
        return self._request("GET", "/api/v1/orderbook", group="MARKET_DATA", params={"symbol": symbol})

    def get_trades(self, symbol: str, count: int = 50) -> list:
        return self._request("GET", "/api/v1/trades", group="MARKET_DATA", params={"symbol": symbol, "count": count})

    def get_candles(self, symbol: str, interval: str, count: int = 100, before: str | None = None) -> dict:
        params = {"symbol": symbol, "interval": interval, "count": count}
        if before:
            params["before"] = before
        return self._request("GET", "/api/v1/candles", group="MARKET_DATA_CHART", params=params)

    # --- stock / market info ---
    def get_stocks(self, symbols: list[str]) -> list:
        return self._request("GET", "/api/v1/stocks", group="STOCK", params={"symbols": ",".join(symbols)})

    def get_exchange_rate(self, base: str, quote: str) -> dict:
        return self._request("GET", "/api/v1/exchange-rate", group="MARKET_INFO",
                             params={"baseCurrency": base, "quoteCurrency": quote})

    def get_market_calendar(self, country: str, date: str | None = None) -> dict:
        params = {"date": date} if date else None
        return self._request("GET", f"/api/v1/market-calendar/{country}", group="MARKET_INFO", params=params)

    # --- order info ---
    def get_buying_power(self, currency: str) -> "BuyingPower":
        from .models import BuyingPower
        result = self._request("GET", "/api/v1/buying-power", group="ORDER_INFO",
                              account=True, params={"currency": currency})
        return BuyingPower.model_validate(result)

    def get_sellable_quantity(self, symbol: str) -> dict:
        return self._request("GET", "/api/v1/sellable-quantity", group="ORDER_INFO",
                            account=True, params={"symbol": symbol})

    def get_commissions(self) -> list:
        return self._request("GET", "/api/v1/commissions", group="ORDER_INFO", account=True)

    # --- order history ---
    def list_orders(self, status: str = "OPEN", symbol: str | None = None, cursor: str | None = None, limit: int = 20) -> dict:
        params = {"status": status, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/api/v1/orders", group="ORDER_HISTORY", account=True, params=params)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/orders/{order_id}", group="ORDER_HISTORY", account=True)

    # --- order write ---
    def place_order(self, *, symbol: str, side: str, order_type: str,
                    quantity: str | None = None, price: str | None = None,
                    order_amount: str | None = None, time_in_force: str = "DAY",
                    client_order_id: str | None = None,
                    confirm_high_value_order: bool = False) -> "OrderResponse":
        from .models import OrderResponse
        payload: dict = {"symbol": symbol, "side": side, "orderType": order_type,
                         "timeInForce": time_in_force, "confirmHighValueOrder": confirm_high_value_order}
        if quantity is not None:
            payload["quantity"] = quantity
        if price is not None:
            payload["price"] = price
        if order_amount is not None:
            payload["orderAmount"] = order_amount
        if client_order_id is not None:
            payload["clientOrderId"] = client_order_id
        result = self._request("POST", "/api/v1/orders", group="ORDER", account=True, json=payload)
        return OrderResponse.model_validate(result)

    def modify_order(self, order_id: str, *, order_type: str, price: str | None = None,
                     quantity: str | None = None, confirm_high_value_order: bool = False) -> dict:
        payload: dict = {"orderType": order_type, "confirmHighValueOrder": confirm_high_value_order}
        if price is not None:
            payload["price"] = price
        if quantity is not None:
            payload["quantity"] = quantity
        return self._request("POST", f"/api/v1/orders/{order_id}/modify", group="ORDER", account=True, json=payload)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/api/v1/orders/{order_id}/cancel", group="ORDER", account=True, json={})
