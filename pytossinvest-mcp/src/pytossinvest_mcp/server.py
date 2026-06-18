from __future__ import annotations

import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from .audit import AuditLog
from .config import Settings
from .paper import PaperBroker
from .safety import SafetyManager
from .tools import AppContext
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
        now=_time.monotonic,
        today=lambda: datetime.now(_KST).date(),
        token_store=token_store,
        spend_store=spend_store,
    )
    safety.restore_spend(audit.read_events())  # memory: rebuild today; redis: seed is no-op
    return AppContext(
        config=settings, client=client, paper=paper, safety=safety, audit=audit,
        now_kst=lambda: datetime.now(_KST),
    )


def build_server(settings: Settings, *, client) -> FastMCP:
    app = build_app_context(settings, client=client)
    mcp = FastMCP("pytossinvest-mcp")
    _register_reads(mcp, app)
    if settings.mode != "read_only":
        _register_writes(mcp, app)
    return mcp


def _register_reads(mcp: FastMCP, app: AppContext) -> None:
    @mcp.tool(name="get_accounts",
              description="List brokerage accounts. Paper mode returns a synthetic PAPER account.")
    def get_accounts() -> dict:
        return T.get_accounts(app)

    @mcp.tool(name="get_holdings",
              description="Current holdings/positions. Money & quantities are strings.")
    def get_holdings(symbol: "str | None" = None) -> dict:
        return T.get_holdings(app, symbol)

    @mcp.tool(name="get_quote",
              description="Latest price(s) for up to 200 symbols; a single symbol also returns "
                          "orderbook & recent trades. All prices are strings.")
    def get_quote(symbols: list[str]) -> dict:
        return T.get_quote(app, symbols)

    @mcp.tool(name="get_candles", description="OHLC candles. interval is '1m' or '1d'.")
    def get_candles(symbol: str, interval: str, count: int = 100,
                    before: "str | None" = None) -> dict:
        return T.get_candles(app, symbol, interval, count, before)

    @mcp.tool(name="get_stock_info", description="Basic stock info for up to 200 symbols.")
    def get_stock_info(symbols: list[str]) -> dict:
        return T.get_stock_info(app, symbols)

    @mcp.tool(name="get_market_info",
              description="Market calendar for a country ('KR'/'US'); optional FX rate when "
                          "base_currency & quote_currency are given.")
    def get_market_info(country: str = "KR", base_currency: "str | None" = None,
                        quote_currency: "str | None" = None) -> dict:
        return T.get_market_info(app, country, base_currency, quote_currency)

    @mcp.tool(name="list_orders",
              description="Open orders (real API returns OPEN only). Paper returns simulated orders.")
    def list_orders(status: str = "OPEN", symbol: "str | None" = None) -> dict:
        return T.list_orders(app, status, symbol)

    @mcp.tool(name="get_order", description="Order detail by id.")
    def get_order(order_id: str) -> dict:
        return T.get_order(app, order_id)


def _register_writes(mcp: FastMCP, app: AppContext) -> None:
    @mcp.tool(name="get_order_readiness",
              description="Buying power, sellable quantity, and commissions before ordering.")
    def get_order_readiness(symbol: str, side: str = "BUY", currency: str = "KRW") -> dict:
        return T.get_order_readiness(app, symbol, side, currency)

    @mcp.tool(name="preview_order",
              description="STEP 1 of 2. Validate an order against guardrails and estimate cost; "
                          "returns a confirmation_token. Money/quantity are strings. Does NOT place "
                          "the order. For a MARKET quantity order, the current price is used to estimate.")
    def preview_order(symbol: str, side: str, order_type: str, quantity: "str | None" = None,
                      price: "str | None" = None, order_amount: "str | None" = None,
                      time_in_force: str = "DAY", confirm_high_value_order: bool = False) -> dict:
        return T.preview_order(
            app, symbol=symbol, side=side, order_type=order_type, quantity=quantity,
            price=price, order_amount=order_amount, time_in_force=time_in_force,
            confirm_high_value_order=confirm_high_value_order,
        )

    @mcp.tool(name="place_order",
              description="STEP 2 of 2. Place the order previously validated by preview_order, using "
                          "its confirmation_token. Idempotent: a failed attempt can be retried with the "
                          "same token.")
    def place_order(confirmation_token: str) -> dict:
        return T.place_order(app, confirmation_token=confirmation_token)

    @mcp.tool(name="preview_modify",
              description="STEP 1 of 2 to modify a LIVE open order. Merges the amendment with the "
                          "original order, validates it against guardrails, and returns a "
                          "confirmation_token. live only. Money/quantity are strings.")
    def preview_modify(order_id: str, order_type: str, price: "str | None" = None,
                       quantity: "str | None" = None, confirm_high_value_order: bool = False) -> dict:
        return T.preview_modify(app, order_id, order_type=order_type, price=price,
                                quantity=quantity, confirm_high_value_order=confirm_high_value_order)

    @mcp.tool(name="modify_order",
              description="STEP 2 of 2. Apply the modification validated by preview_modify, using its "
                          "confirmation_token (returns a NEW orderId). live only; idempotent.")
    def modify_order(confirmation_token: str) -> dict:
        return T.modify_order(app, confirmation_token=confirmation_token)

    @mcp.tool(name="cancel_order",
              description="Cancel an open order (live only; returns a NEW orderId).")
    def cancel_order(order_id: str) -> dict:
        return T.cancel_order(app, order_id)


def main() -> None:
    settings = Settings()
    from pytossinvest import TossInvestClient

    client = TossInvestClient(
        settings.client_id, settings.client_secret, base_url=settings.base_url
    )
    mcp = build_server(settings, client=client)
    mcp.run()  # stdio transport (default) for MCP clients like Claude Desktop
