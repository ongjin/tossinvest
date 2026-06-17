from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .config import Settings
from .paper import PaperBroker, PaperOrder
from .safety import SafetyManager

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class AppContext:
    config: Settings
    client: Any  # TossInvestClient or compatible
    paper: PaperBroker
    safety: SafetyManager
    audit: AuditLog
    now_kst: Callable[[], datetime] = lambda: datetime.now(_KST)

    @property
    def use_paper(self) -> bool:
        return self.config.use_paper

    @property
    def is_live(self) -> bool:
        return self.config.is_live


def _paper_order_dict(o: PaperOrder) -> dict:
    return {
        "orderId": o.order_id,
        "symbol": o.symbol,
        "side": o.side,
        "orderType": o.order_type,
        "quantity": str(o.quantity),
        "price": str(o.price),
        "status": o.status,
        "clientOrderId": o.client_order_id,
    }


# --- market data (always the real client) ---

def get_quote(app: AppContext, symbols: list[str]) -> dict:
    prices = app.client.get_prices(symbols)
    out: dict = {
        "prices": [
            {"symbol": p.symbol, "lastPrice": str(p.last_price), "currency": p.currency}
            for p in prices
        ]
    }
    if len(symbols) == 1:
        out["orderbook"] = app.client.get_orderbook(symbols[0])
        out["trades"] = app.client.get_trades(symbols[0])
    return out


def get_candles(app: AppContext, symbol: str, interval: str,
                count: int = 100, before: str | None = None) -> dict:
    return app.client.get_candles(symbol, interval, count=count, before=before)


def get_stock_info(app: AppContext, symbols: list[str]) -> dict:
    return {"stocks": app.client.get_stocks(symbols)}


def get_market_info(app: AppContext, country: str = "KR",
                    base_currency: str | None = None,
                    quote_currency: str | None = None) -> dict:
    out: dict = {"calendar": app.client.get_market_calendar(country)}
    if base_currency and quote_currency:
        out["exchangeRate"] = app.client.get_exchange_rate(base_currency, quote_currency)
    return out


# --- account / order reads (paper-routed) ---

def get_accounts(app: AppContext) -> dict:
    if app.use_paper:
        return {"accounts": [{"accountNo": "PAPER", "accountSeq": 0, "accountType": "PAPER"}]}
    return {"accounts": [a.model_dump(by_alias=True) for a in app.client.get_accounts()]}


def get_holdings(app: AppContext, symbol: str | None = None) -> dict:
    if app.use_paper:
        return app.paper.holdings()
    return app.client.get_holdings(symbol)


def list_orders(app: AppContext, status: str = "OPEN", symbol: str | None = None) -> dict:
    if app.use_paper:
        items = [_paper_order_dict(o) for o in app.paper.list_orders()
                 if symbol is None or o.symbol == symbol]
        return {"orders": items, "hasNext": False}
    return app.client.list_orders(status=status, symbol=symbol)


def get_order(app: AppContext, order_id: str) -> dict:
    if app.use_paper:
        o = app.paper.get_order(order_id)
        if o is None:
            raise ValueError(f"paper order not found: {order_id}")
        return _paper_order_dict(o)
    return app.client.get_order(order_id)
