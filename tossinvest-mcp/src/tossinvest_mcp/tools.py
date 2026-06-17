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


# --- write tools (readiness, preview -> place, modify/cancel) ---

from decimal import Decimal  # noqa: E402  (appended section)

from pytossinvest.money import to_decimal  # noqa: E402
from . import market_hours  # noqa: E402


def _market_gate(app: AppContext, symbol: str) -> "tuple[bool, bool]":
    """Return (is_market_open, enforce_hours). Hours are enforced only in live mode."""
    enforce = app.config.enforce_market_hours and app.is_live
    if not enforce:
        return True, False
    country = "US" if symbol.isalpha() else "KR"
    cal = app.client.get_market_calendar(country)
    return market_hours.is_market_open(cal, app.now_kst(), country), True


def _ref_price(app: AppContext, symbol: str) -> "str | None":
    prices = app.client.get_prices([symbol])
    return str(prices[0].last_price) if prices else None


def get_order_readiness(app: AppContext, symbol: str, side: str = "BUY",
                        currency: str = "KRW") -> dict:
    if app.use_paper:
        return {
            "buyingPower": str(app.paper.buying_power()),
            "sellableQuantity": str(app.paper.sellable_quantity(symbol)),
            "commissions": [],
        }
    bp = app.client.get_buying_power(currency)
    return {
        "buyingPower": {"currency": bp.currency, "cashBuyingPower": str(bp.cash_buying_power)},
        "sellableQuantity": app.client.get_sellable_quantity(symbol),
        "commissions": app.client.get_commissions(),
    }


def preview_order(app: AppContext, *, symbol: str, side: str, order_type: str,
                  quantity: "str | None" = None, price: "str | None" = None,
                  order_amount: "str | None" = None, time_in_force: str = "DAY",
                  confirm_high_value_order: bool = False) -> dict:
    ref = None
    if order_type == "MARKET" and order_amount is None:
        ref = _ref_price(app, symbol)
    spec = app.safety.build_spec(
        symbol=symbol, side=side, order_type=order_type, quantity=quantity, price=price,
        order_amount=order_amount, time_in_force=time_in_force,
        confirm_high_value_order=confirm_high_value_order, ref_price=ref,
    )
    is_open, enforce = _market_gate(app, symbol)
    app.safety.check_guardrails(spec, is_market_open=is_open, enforce_hours=enforce)
    token = app.safety.issue_token(spec)
    app.audit.record({
        "tool": "preview_order", "mode": app.config.mode, "decision": "previewed",
        "symbol": symbol, "side": side, "notional": spec.notional,
        "clientOrderId": spec.client_order_id, "token": token,
    })
    return {
        "confirmationToken": token,
        "clientOrderId": spec.client_order_id,
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "estimatedNotional": str(spec.notional),
        "expiresInSec": app.config.confirmation_ttl_sec,
        "mode": app.config.mode,
    }


def place_order(app: AppContext, *, confirmation_token: str) -> dict:
    spec = app.safety.consume(confirmation_token)  # validates exists + not expired
    try:
        if app.use_paper:
            if spec.price is not None:
                fill_price = spec.price
                qty = spec.quantity
            else:
                from .paper import PaperError
                fill_price = _ref_price(app, spec.symbol)
                if not fill_price or to_decimal(fill_price) <= 0:
                    raise PaperError(
                        f"no reference price available for MARKET fill of {spec.symbol}; retry"
                    )
                if spec.quantity is not None:
                    qty = spec.quantity
                else:  # US amount-based: qty = amount / price
                    qty = str(to_decimal(spec.order_amount) / to_decimal(fill_price))
            order = app.paper.place(
                symbol=spec.symbol, side=spec.side, order_type=spec.order_type,
                fill_price=fill_price, quantity=qty, client_order_id=spec.client_order_id,
            )
            result = _paper_order_dict(order)
        else:
            resp = app.client.place_order(
                symbol=spec.symbol, side=spec.side, order_type=spec.order_type,
                quantity=spec.quantity, price=spec.price, order_amount=spec.order_amount,
                time_in_force=spec.time_in_force, client_order_id=spec.client_order_id,
                confirm_high_value_order=spec.confirm_high_value_order,
            )
            result = {"orderId": resp.order_id, "clientOrderId": resp.client_order_id}
    except Exception as e:
        app.audit.record({
            "tool": "place_order", "mode": app.config.mode, "decision": "error",
            "error": str(e), "clientOrderId": spec.client_order_id,
        })
        raise  # token NOT finalized -> retry reuses same clientOrderId (idempotent)

    app.safety.finalize(confirmation_token, spec.notional)
    app.audit.record({
        "tool": "place_order", "mode": app.config.mode, "decision": "placed",
        "result": result, "clientOrderId": spec.client_order_id,
    })
    return result


def modify_order(app: AppContext, order_id: str, *, order_type: str,
                 price: "str | None" = None, quantity: "str | None" = None,
                 confirm_high_value_order: bool = False) -> dict:
    if app.use_paper:
        from .paper import PaperError
        raise PaperError("paper mode fills orders immediately; modify is live-only")
    result = app.client.modify_order(
        order_id, order_type=order_type, price=price, quantity=quantity,
        confirm_high_value_order=confirm_high_value_order,
    )
    app.audit.record({"tool": "modify_order", "mode": app.config.mode,
                      "decision": "modified", "orderId": order_id, "result": result})
    return result


def cancel_order(app: AppContext, order_id: str) -> dict:
    if app.use_paper:
        from .paper import PaperError
        raise PaperError("paper mode fills orders immediately; cancel is live-only")
    result = app.client.cancel_order(order_id)
    app.audit.record({"tool": "cancel_order", "mode": app.config.mode,
                      "decision": "canceled", "orderId": order_id, "result": result})
    return result
