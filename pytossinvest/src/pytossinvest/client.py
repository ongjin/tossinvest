from __future__ import annotations

import math
import random
import threading
import time as _time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .auth import TokenCache, TokenManager
from .errors import error_from_response, TossInvestError
from .ratelimit import TokenBucket, effective_rate, backoff_wait

__all__ = ["TossInvestClient"]

_KST = ZoneInfo("Asia/Seoul")

# Seed TPS per group: static documented defaults used until the server's
# X-RateLimit-* headers are seen, which then reconcile the bucket (header is the
# source of truth) and suppress peak-halving for that group. 429s are auto-retried
# (bounded); RateLimitError surfaces only once retries are exhausted.
_GROUP_RATES: dict[str, float] = {
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "ORDER": 6,
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,
    "CONDITIONAL_ORDER": 5,
    "CONDITIONAL_ORDER_HISTORY": 10,
    "STOCK_ALL": 1,
    "STOCK_TRADING_TREND": 10,
    "RANKING": 5,
    "MARKET_INDICATOR": 10,
    "MARKET_INDICATOR_CHART": 5,
}

# 401 codes where a fresh token fixes the call. token-revoked: the API keeps one live token
# per client, so another process issuing a token for the same client kills ours.
_REISSUE_TOKEN_CODES = frozenset({"expired-token", "token-revoked"})

# A re-issued token can be revoked again before the retry lands when another process on the
# same client id is issuing too, so one more round is allowed after a short random pause that
# lets the two fall out of step.
_MAX_TOKEN_REISSUES = 2
_REISSUE_JITTER_SEC = 0.5

# httpx drops an idle connection after 5s. Calls driven by an LLM or a person arrive further
# apart than that, so each one paid a new TCP+TLS handshake. The two pool caps restate httpx's
# client defaults: a bare httpx.Limits() would lift them to "unlimited".
_HTTP_LIMITS = httpx.Limits(
    max_connections=100, max_keepalive_connections=20, keepalive_expiry=60.0
)

# A caller-supplied path value is refused, not encoded, when it could change which endpoint
# is addressed. "." and ".." navigate the path and "" removes the segment. The characters
# would restructure the path if a server or proxy decoded them again ("/", "\\", "%"), start
# a query or fragment ("?", "#") or carry path parameters (";"): what the far side does with
# %2F is not something the client can know.
_UNSAFE_PATH_VALUES = frozenset({"", ".", ".."})
_PATH_STRUCTURE_CHARS = frozenset("/\\%?#;")


def _drop_none(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


def _path_seg(value: str) -> str:
    """A caller-supplied id or symbol as exactly one URL path segment. Values that could
    reach another endpoint raise ValueError; other reserved characters are percent-encoded."""
    text = str(value)
    unsafe = (
        text in _UNSAFE_PATH_VALUES
        or not _PATH_STRUCTURE_CHARS.isdisjoint(text)
        or any(ch.isspace() or not ch.isprintable() for ch in text)
    )
    if unsafe:
        raise ValueError(f"invalid path value: {text!r}")
    return quote(text, safe="")


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
        max_retries: int = 3,
        retry_max_wait: float = 60.0,
        rng: Callable[[], float] = random.random,
        account_seq: int | None = None,
        token_cache: TokenCache | None = None,
    ):
        """account_seq picks the account that account-scoped calls use; left unset, the first
        account from get_accounts() is used. One client may be shared between threads.
        token_cache lets several processes on one client id share its single live token."""
        self._http = httpx.Client(base_url=base_url, timeout=timeout, limits=_HTTP_LIMITS)
        self._token = TokenManager(client_id, client_secret, http=self._http, now=monotonic,
                                   cache=token_cache)
        self._sleep = sleep
        self._now_kst = now_kst
        self._max_retries = max_retries
        self._retry_max_wait = retry_max_wait
        self._rng = rng
        self._buckets: dict[str, TokenBucket] = {
            g: TokenBucket(capacity=r, refill_per_sec=r, now=monotonic)
            for g, r in _GROUP_RATES.items()
        }
        self._account_seq: int | None = account_seq
        self._account_lock = threading.Lock()
        self._rate_from_header: dict[str, bool] = {}
        self._rate_lock = threading.Lock()  # guards the buckets and _rate_from_header

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
        while True:
            with self._rate_lock:
                # Once the server's X-RateLimit-* headers have been seen for this group, they
                # are the source of truth (they already reflect the 09:00-09:10 peak halving),
                # so don't re-apply the static effective_rate. Until then, use the documented
                # default.
                if not self._rate_from_header.get(group):
                    rate = effective_rate(group, _GROUP_RATES[group], self._now_kst())
                    bucket.capacity = rate
                    bucket.refill_per_sec = rate
                if bucket.try_acquire():
                    return
                wait = bucket.time_until_available()

            self._sleep(wait)  # outside the lock: other threads keep moving

    def _sync_bucket_from_headers(self, group: str, headers) -> None:
        """Reconcile a group's bucket to the server's X-RateLimit-* headers (source of truth).
        No-op if any header is absent, unparseable or nonsensical."""
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        limit = headers.get("X-RateLimit-Limit")
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if limit is None or remaining is None or reset is None:
            return
        try:
            limit_f = float(limit)
            remaining_f = float(remaining)
            reset_f = float(reset)
        except (TypeError, ValueError):
            return
        # A bucket that can never hand out a token (capacity below one request, NaN, a zero
        # refill rate) would park every caller in the gate for good.
        if not all(map(math.isfinite, (limit_f, remaining_f, reset_f))):
            return
        if limit_f < 1 or remaining_f < 0 or reset_f < 0:
            return

        # Never wait in the gate longer than a 429 retry would: past retry_max_wait the request
        # is sent anyway and the bounded 429 path takes over.
        refill_period = min(reset_f, self._retry_max_wait)

        with self._rate_lock:
            bucket.capacity = limit_f
            if refill_period > 0:
                bucket.refill_per_sec = 1.0 / refill_period
            bucket._tokens = min(bucket._tokens, remaining_f)
            self._rate_from_header[group] = True

    def _request(
        self,
        method: str,
        path: str,
        *,
        group: str,
        account: bool = False,
        params: dict | None = None,
        json: dict | None = None,
        _reissues: int = 0,
        _attempt: int = 0,
    ) -> Any:
        if account and self._account_seq is None:
            with self._account_lock:  # threads arriving together share one lookup
                if self._account_seq is None:
                    self.get_accounts()  # caches the first account's accountSeq
            if self._account_seq is None:
                raise RuntimeError("account context required but the API returned no account")
        self._gate(group)
        token = self._token.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account:
            headers["X-Tossinvest-Account"] = str(self._account_seq)

        resp = self._http.request(method, path, params=params, json=json, headers=headers)
        self._sync_bucket_from_headers(group, resp.headers)

        if resp.status_code == 200:
            try:
                body = resp.json()
            except (ValueError, RecursionError):
                raise TossInvestError(
                    "invalid-response", "200 response body was not valid JSON",
                    http_status=200,
                )
            if not isinstance(body, dict) or "result" not in body:
                raise TossInvestError(
                    "missing-result", "200 response had no 'result' field",
                    http_status=200,
                )
            return body["result"]

        if resp.status_code == 204:  # no body and no result envelope (conditional-order DELETE)
            return None

        try:
            body = resp.json()
        except ValueError:
            body = {}
        error = error_from_response(resp.status_code, body, resp.headers)

        reissue = (
            resp.status_code == 401
            and _reissues < _MAX_TOKEN_REISSUES
            and error.code in _REISSUE_TOKEN_CODES
        )
        if reissue:
            self._token.invalidate(token)  # a no-op if another thread already re-issued
            if _reissues:
                self._sleep(self._rng() * _REISSUE_JITTER_SEC)
            return self._request(
                method, path, group=group, account=account,
                params=params, json=json, _reissues=_reissues + 1, _attempt=_attempt,
            )

        if resp.status_code == 429 and _attempt < self._max_retries:
            retry_after = None
            raw = resp.headers.get("Retry-After")
            if raw is not None:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
            self._sleep(backoff_wait(_attempt, retry_after,
                                     cap=self._retry_max_wait, rng=self._rng))
            return self._request(
                method, path, group=group, account=account,
                params=params, json=json, _reissues=_reissues, _attempt=_attempt + 1,
            )

        raise error

    def access_token(self) -> str:
        """A current OAuth access token (cached, refreshed when near expiry), e.g. for the
        WebSocket handshake in pytossinvest.stream."""
        return self._token.get_token()

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
        return self._request("GET", f"/api/v1/market-calendar/{_path_seg(country)}", group="MARKET_INFO", params=params)

    def get_all_stocks(self, market: str, *, status: str | None = None,
                       security_type: str | None = None, common_share: bool | None = None) -> list:
        """Every symbol of one market (KOSPI, KOSDAQ, NYSE, NASDAQ, AMEX, KR_ETC, US_ETC). Large."""
        params = _drop_none({"market": market, "status": status, "securityType": security_type,
                             "commonShare": common_share})
        return self._request("GET", "/api/v1/stocks/all", group="STOCK_ALL", params=params)

    # --- KR trading trends (newest first; page with until=nextUntil) ---
    def _trading_trend(self, kind: str, symbol: str, count: int, until: str | None) -> dict:
        params = _drop_none({"count": count, "until": until})
        return self._request("GET", f"/api/v1/stocks/{_path_seg(symbol)}/{kind}", group="STOCK_TRADING_TREND",
                             params=params)

    def get_investor_trading(self, symbol: str, count: int = 10, until: str | None = None) -> dict:
        return self._trading_trend("investor-trading", symbol, count, until)

    def get_program_trades(self, symbol: str, count: int = 10, until: str | None = None) -> dict:
        return self._trading_trend("program-trades", symbol, count, until)

    def get_short_selling(self, symbol: str, count: int = 10, until: str | None = None) -> dict:
        return self._trading_trend("short-selling", symbol, count, until)

    def get_credit_trades(self, symbol: str, count: int = 10, until: str | None = None) -> dict:
        return self._trading_trend("credit-trades", symbol, count, until)

    def get_securities_lending(self, symbol: str, count: int = 10, until: str | None = None) -> dict:
        return self._trading_trend("securities-lending", symbol, count, until)

    def get_rankings(self, type: str, market_country: str, duration: str, *,
                     exclude_investment_caution: bool = False, count: int = 100) -> dict:
        params = {"type": type, "marketCountry": market_country, "duration": duration,
                  "excludeInvestmentCaution": exclude_investment_caution, "count": count}
        return self._request("GET", "/api/v1/rankings", group="RANKING", params=params)

    # --- market indicators (KOSPI, KOSDAQ, KR_BOND_2Y..30Y) ---
    def get_indicator_prices(self, symbols: list[str]) -> list:
        return self._request("GET", "/api/v1/market-indicators/prices", group="MARKET_INDICATOR",
                             params={"symbols": ",".join(symbols)})

    def get_indicator_candles(self, symbol: str, interval: str, count: int = 100,
                              before: str | None = None) -> dict:
        params = _drop_none({"interval": interval, "count": count, "before": before})
        return self._request("GET", f"/api/v1/market-indicators/{_path_seg(symbol)}/candles",
                             group="MARKET_INDICATOR_CHART", params=params)

    def get_index_investor_trading(self, symbol: str, interval: str, count: int = 10,
                                   until: str | None = None) -> dict:
        """Trading value by investor type for KOSPI or KOSDAQ."""
        params = _drop_none({"interval": interval, "count": count, "until": until})
        return self._request("GET", f"/api/v1/market-indicators/{_path_seg(symbol)}/investor-trading",
                             group="MARKET_INDICATOR", params=params)

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
        return self._request("GET", f"/api/v1/orders/{_path_seg(order_id)}", group="ORDER_HISTORY", account=True)

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
        return self._request("POST", f"/api/v1/orders/{_path_seg(order_id)}/modify", group="ORDER", account=True, json=payload)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/api/v1/orders/{_path_seg(order_id)}/cancel", group="ORDER", account=True, json={})

    # --- conditional orders ---
    # first/second use the wire shape {"orderSide", "triggerPrice", "orderPrice"?} with string prices.
    @staticmethod
    def _conditional_payload(type: str, quantity: str, order_type: str, expire_date: str,
                             first: dict, second: dict | None, confirm_high_value_order: bool) -> dict:
        payload: dict = {"type": type, "quantity": quantity, "orderType": order_type,
                         "expireDate": expire_date, "first": first,
                         "confirmHighValueOrder": confirm_high_value_order}
        if second is not None:
            payload["second"] = second
        return payload

    def create_conditional_order(self, *, symbol: str, type: str, quantity: str, order_type: str,
                                 expire_date: str, first: dict, second: dict | None = None,
                                 client_order_id: str | None = None,
                                 confirm_high_value_order: bool = False) -> dict:
        """Register a SINGLE / OCO / OTO order that fires when its trigger price is reached."""
        payload = self._conditional_payload(type, quantity, order_type, expire_date, first, second,
                                            confirm_high_value_order)
        payload["symbol"] = symbol
        if client_order_id is not None:
            payload["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/conditional-orders", group="CONDITIONAL_ORDER",
                             account=True, json=payload)

    def modify_conditional_order(self, conditional_order_id: str, *, type: str, quantity: str,
                                 order_type: str, expire_date: str, first: dict,
                                 second: dict | None = None,
                                 confirm_high_value_order: bool = False) -> dict:
        """Replace the whole order. The API cancels and re-creates it: the returned id is new."""
        payload = self._conditional_payload(type, quantity, order_type, expire_date, first, second,
                                            confirm_high_value_order)
        return self._request("POST", f"/api/v1/conditional-orders/{_path_seg(conditional_order_id)}/modify",
                             group="CONDITIONAL_ORDER", account=True, json=payload)

    def cancel_conditional_order(self, conditional_order_id: str) -> None:
        self._request("DELETE", f"/api/v1/conditional-orders/{_path_seg(conditional_order_id)}",
                      group="CONDITIONAL_ORDER", account=True)

    def list_conditional_orders(self, status: str = "OPEN", symbol: str | None = None,
                                cursor: str | None = None, limit: int = 20) -> dict:
        """Includes conditional orders made in other channels (e.g. the app)."""
        params = {"status": status, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/api/v1/conditional-orders", group="CONDITIONAL_ORDER_HISTORY",
                             account=True, params=params)

    def get_conditional_order(self, conditional_order_id: str) -> dict:
        return self._request("GET", f"/api/v1/conditional-orders/{_path_seg(conditional_order_id)}",
                             group="CONDITIONAL_ORDER_HISTORY", account=True)
