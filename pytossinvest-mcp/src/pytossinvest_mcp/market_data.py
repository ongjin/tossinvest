"""Market data beyond quotes: KR trading trends, rankings and market indicators.

Account-free, so every mode reads the real API (like get_quote). The five KR trading-trend
endpoints fold into one tool, and indicator candles / investor trading into another, to keep
the tool list short.
"""
from __future__ import annotations

_TREND_METHODS = {
    "investor": "get_investor_trading",
    "program": "get_program_trades",
    "short_selling": "get_short_selling",
    "credit": "get_credit_trades",
    "lending": "get_securities_lending",
}
DEFAULT_RANKING_COUNT = 20  # the API default (100) is a lot of context for one call
CANDLES = "candles"


def _given(**kwargs) -> dict:
    return {k: v for k, v in kwargs.items() if v is not None}


def get_stock_trends(app, symbol: str, kind: str, count: int = 10, until: str | None = None) -> dict:
    method = getattr(app.client, _TREND_METHODS[kind])
    return method(symbol, count=count, until=until)


def get_rankings(app, type: str, market_country: str, duration: str,
                 exclude_investment_caution: bool = False,
                 count: int = DEFAULT_RANKING_COUNT) -> dict:
    return app.client.get_rankings(type, market_country, duration,
                                   exclude_investment_caution=exclude_investment_caution,
                                   count=count)


def get_market_indicators(app, symbols: list[str]) -> dict:
    return {"indicators": app.client.get_indicator_prices(symbols)}


def get_indicator_history(app, symbol: str, view: str, interval: str, count: int | None = None,
                          before: str | None = None, until: str | None = None) -> dict:
    """candles page back with before=nextBefore; investor_trading with until=nextUntil."""
    if view == CANDLES:
        if until is not None:
            raise ValueError("candles page with before; until belongs to investor_trading")
        return app.client.get_indicator_candles(symbol, interval, **_given(count=count, before=before))
    if before is not None:
        raise ValueError("investor_trading pages with until; before belongs to candles")
    return app.client.get_index_investor_trading(symbol, interval, **_given(count=count, until=until))
