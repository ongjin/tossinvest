from __future__ import annotations

import functools
import time as _time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pytossinvest.errors import TossInvestError

from .audit import AuditLog
from .config import Settings
from .paper import PaperBroker, PaperError
from .safety import GuardrailError, SafetyManager
from .tools import AppContext
from . import __version__
from . import tools as T

_KST = ZoneInfo("Asia/Seoul")


def _redis_from_url(url: str):
    import redis  # optional dependency — only imported for the redis backend
    return redis.Redis.from_url(url, decode_responses=True)


def _build_stores(settings: Settings):
    if settings.state_backend == "redis":
        from .redis_stores import RedisTokenStore, RedisSpendStore, RedisPaperStore
        from .audit import RedisAuditSink
        r = _redis_from_url(settings.redis_url)
        return (RedisTokenStore(r), RedisSpendStore(r),
                RedisAuditSink(r),
                RedisPaperStore(r, starting_cash=settings.paper_starting_cash))
    from .stores import MemoryTokenStore, MemorySpendStore
    from .paper import MemoryPaperStore
    return (MemoryTokenStore(), MemorySpendStore(),
            AuditLog(settings.audit_log_path),
            MemoryPaperStore(starting_cash=settings.paper_starting_cash))


def build_app_context(settings: Settings, *, client) -> AppContext:
    token_store, spend_store, audit, paper_store = _build_stores(settings)
    paper = PaperBroker(paper_store)
    safety = SafetyManager(
        settings,
        now=_time.time,
        today=lambda: datetime.now(_KST).date(),
        token_store=token_store,
        spend_store=spend_store,
    )
    safety.restore_spend(audit.read_events())  # memory: rebuild today; redis: seed is no-op
    return AppContext(
        config=settings, client=client, paper=paper, safety=safety, audit=audit,
        now_kst=lambda: datetime.now(_KST),
    )


def build_server(settings: Settings, *, client, cache_hints=None) -> MCPServer:
    app = build_app_context(settings, client=client)
    mcp = MCPServer("pytossinvest-mcp", version=__version__, cache_hints=cache_hints)
    _register_reads(mcp, app)
    if settings.mode != "read_only":
        _register_writes(mcp, app)
    return mcp


def transport_kwargs(settings: Settings) -> dict:
    """Streamable-HTTP transport options. SDK v2 moved these off the server object and
    onto streamable_http_app(), so they are computed here and applied at mount time."""
    # The bearer middleware (http.py) is the entire auth surface; the deploy host is
    # operator/proxy-controlled and unknown at build time, so the SDK's localhost-only
    # DNS-rebinding default would 421 every real remote client. Auth = bearer, not Host.
    # Operators who know their host can opt into pinning via http_allowed_hosts
    # (defense-in-depth: re-enables the Host check against the allowlist).
    from mcp.server.transport_security import TransportSecuritySettings
    if settings.http_allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.http_allowed_hosts,
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # v2 defaults stateless_http to False even though the 2026-07-28 core is sessionless;
    # this server has always run sessionless over HTTP, so opt in explicitly.
    return {"stateless_http": True, "transport_security": security}


# Advertised as JSON-schema enums so a client can't send e.g. side="buy" (paper would
# accept the preview and only fail at place; Toss rejects it with a 400).
Side = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "MARKET"]
TimeInForce = Literal["DAY", "CLS", "OPG"]
OrderStatus = Literal["OPEN", "CLOSED"]
Currency = Literal["KRW", "USD"]


# Exceptions whose text is written for the model. SDK >= 2.1 hides the text of any other
# exception ("Error executing tool <name>"), which would swallow guardrail/Toss reasons.
# httpx errors are included so a timed-out place/modify reads as retryable with its token.
_MODEL_FACING_ERRORS = (GuardrailError, PaperError, TossInvestError, ValueError, httpx.HTTPError)


def _model_facing(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _MODEL_FACING_ERRORS as e:
            raise ToolError(str(e)) from e
    return wrapper


def _model_facing_tool(mcp: MCPServer):
    """Drop-in for mcp.tool(...) that surfaces domain errors to the model as ToolError."""
    return lambda **kwargs: lambda fn: mcp.tool(**kwargs)(_model_facing(fn))


def _register_reads(mcp: MCPServer, app: AppContext) -> None:
    mcp_tool = _model_facing_tool(mcp)

    @mcp_tool(name="get_accounts",
              description="List brokerage accounts. Paper mode returns a synthetic PAPER account.")
    def get_accounts() -> dict:
        return T.get_accounts(app)

    @mcp_tool(name="get_holdings",
              description="Current holdings/positions. Money & quantities are strings. "
                          "Does not include cash; use get_buying_power for that.")
    def get_holdings(symbol: "str | None" = None) -> dict:
        return T.get_holdings(app, symbol)

    @mcp_tool(name="get_buying_power",
              description="Cash buying power (orderable cash, excluding margin) per currency. "
                          "Omit currency for both KRW and USD. The API has no total-deposit "
                          "figure. Amounts are strings.")
    def get_buying_power(currency: Currency | None = None) -> dict:
        return T.get_buying_power(app, currency)

    @mcp_tool(name="get_quote",
              description="Latest price(s) for up to 200 symbols; a single symbol also returns "
                          "orderbook & recent trades. All prices are strings.")
    def get_quote(symbols: list[str]) -> dict:
        return T.get_quote(app, symbols)

    @mcp_tool(name="get_candles", description="OHLC candles. interval is '1m' or '1d'.")
    def get_candles(symbol: str, interval: str, count: int = 100,
                    before: "str | None" = None) -> dict:
        return T.get_candles(app, symbol, interval, count, before)

    @mcp_tool(name="get_stock_info", description="Basic stock info for up to 200 symbols.")
    def get_stock_info(symbols: list[str]) -> dict:
        return T.get_stock_info(app, symbols)

    @mcp_tool(name="get_market_info",
              description="Market calendar for a country ('KR'/'US'); optional FX rate when "
                          "base_currency & quote_currency are given.")
    def get_market_info(country: str = "KR", base_currency: "str | None" = None,
                        quote_currency: "str | None" = None) -> dict:
        return T.get_market_info(app, country, base_currency, quote_currency)

    @mcp_tool(name="list_orders",
              description="Orders by status: OPEN (unfilled, all at once) or CLOSED (filled/"
                          "canceled, paged: pass the previous nextCursor as cursor while hasNext). "
                          "Paper returns simulated orders.")
    def list_orders(status: OrderStatus = "OPEN", symbol: "str | None" = None,
                    cursor: "str | None" = None) -> dict:
        return T.list_orders(app, status, symbol, cursor)

    @mcp_tool(name="get_order", description="Order detail by id.")
    def get_order(order_id: str) -> dict:
        return T.get_order(app, order_id)


def _register_writes(mcp: MCPServer, app: AppContext) -> None:
    mcp_tool = _model_facing_tool(mcp)

    @mcp_tool(name="get_order_readiness",
              description="Buying power, sellable quantity, and commissions before ordering.")
    def get_order_readiness(symbol: str, side: Side = "BUY", currency: Currency = "KRW") -> dict:
        return T.get_order_readiness(app, symbol, side, currency)

    @mcp_tool(name="preview_order",
              description="STEP 1 of 2. Validate an order against guardrails and estimate cost; "
                          "returns a confirmation_token. Money/quantity are strings. Does NOT place "
                          "the order. For a MARKET quantity order, the current price is used to estimate. "
                          "Size orders in whole shares with quantity (plus price for LIMIT). order_amount "
                          "is a US MARKET dollar-amount order that buys fractional shares; use it only "
                          "when the user asks for an amount.")
    def preview_order(symbol: str, side: Side, order_type: OrderType, quantity: "str | None" = None,
                      price: "str | None" = None, order_amount: "str | None" = None,
                      time_in_force: TimeInForce = "DAY", confirm_high_value_order: bool = False) -> dict:
        return T.preview_order(
            app, symbol=symbol, side=side, order_type=order_type, quantity=quantity,
            price=price, order_amount=order_amount, time_in_force=time_in_force,
            confirm_high_value_order=confirm_high_value_order,
        )

    @mcp_tool(name="place_order",
              description="STEP 2 of 2. Place the order previously validated by preview_order, using "
                          "its confirmation_token. Idempotent: a failed attempt can be retried with the "
                          "same token.")
    def place_order(confirmation_token: str) -> dict:
        return T.place_order(app, confirmation_token=confirmation_token)

    @mcp_tool(name="preview_modify",
              description="STEP 1 of 2 to modify a LIVE open order. Merges the amendment with the "
                          "original order, validates it against guardrails, and returns a "
                          "confirmation_token. live only. Money/quantity are strings.")
    def preview_modify(order_id: str, order_type: OrderType, price: "str | None" = None,
                       quantity: "str | None" = None, confirm_high_value_order: bool = False) -> dict:
        return T.preview_modify(app, order_id, order_type=order_type, price=price,
                                quantity=quantity, confirm_high_value_order=confirm_high_value_order)

    @mcp_tool(name="modify_order",
              description="STEP 2 of 2. Apply the modification validated by preview_modify, using its "
                          "confirmation_token (returns a NEW orderId). live only; idempotent.")
    def modify_order(confirmation_token: str) -> dict:
        return T.modify_order(app, confirmation_token=confirmation_token)

    @mcp_tool(name="cancel_order",
              description="Cancel an open order (live only; returns a NEW orderId).")
    def cancel_order(order_id: str) -> dict:
        return T.cancel_order(app, order_id)


def run_server(settings: Settings, mcp) -> None:
    if settings.transport == "http":
        from .http import build_http_app, serve_http
        app = build_http_app(mcp, auth_token=settings.auth_token,
                             **transport_kwargs(settings))
        serve_http(app, host=settings.http_host, port=settings.http_port)
    else:
        mcp.run()  # stdio transport (default) for MCP clients like Claude Desktop


def main() -> None:
    settings = Settings()
    from pytossinvest import TossInvestClient

    client = TossInvestClient(
        settings.client_id, settings.client_secret, base_url=settings.base_url
    )
    mcp = build_server(settings, client=client)
    run_server(settings, mcp)
