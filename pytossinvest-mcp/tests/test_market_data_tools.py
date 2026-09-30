import pytest

import pytossinvest_mcp.market_data as M


@pytest.mark.parametrize("kind, path", [
    ("investor", "investor-trading"), ("program", "program-trades"),
    ("short_selling", "short-selling"), ("credit", "credit-trades"),
    ("lending", "securities-lending"),
])
def test_stock_trends_route_each_kind(app_factory, fake_client, kind, path):
    app = app_factory(mode="paper")   # account-free market data reads the real API in every mode
    out = M.get_stock_trends(app, "005930", kind, count=5, until="2026-09-01")
    assert out["records"]
    assert ("trend", path, "005930", 5, "2026-09-01") in fake_client.calls


def test_rankings_default_to_twenty(app_factory, fake_client):
    app = app_factory(mode="read_only")
    M.get_rankings(app, "TOP_GAINERS", "KR", "1d")
    assert ("get_rankings", "TOP_GAINERS", "KR", "1d", False, 20) in fake_client.calls


def test_market_indicators(app_factory):
    app = app_factory(mode="read_only")
    assert M.get_market_indicators(app, ["KOSPI"]) == {"indicators": [{"symbol": "KOSPI", "lastPrice": "1"}]}


def test_indicator_history_views(app_factory, fake_client):
    app = app_factory(mode="read_only")
    M.get_indicator_history(app, "KOSPI", "candles", "1d", before="2026-09-01T00:00:00+09:00")
    M.get_indicator_history(app, "KOSDAQ", "investor_trading", "1w", count=4)
    assert ("get_indicator_candles", "KOSPI", "1d", 100, "2026-09-01T00:00:00+09:00") in fake_client.calls
    assert ("get_index_investor_trading", "KOSDAQ", "1w", 4, None) in fake_client.calls


@pytest.mark.parametrize("view, kw", [
    ("candles", {"until": "2026-09-01"}),
    ("investor_trading", {"before": "2026-09-01T00:00:00+09:00"}),
])
def test_indicator_history_rejects_the_other_views_cursor(app_factory, view, kw):
    app = app_factory(mode="read_only")
    with pytest.raises(ValueError):
        M.get_indicator_history(app, "KOSPI", view, "1d", **kw)
