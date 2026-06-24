import pytossinvest_mcp.tools as T


def test_get_quote_single_symbol_includes_depth(app_factory, fake_client):
    app = app_factory(mode="read_only")
    out = T.get_quote(app, ["005930"])
    assert out["prices"][0] == {"symbol": "005930", "lastPrice": "70000", "currency": "KRW"}
    assert "orderbook" in out and "trades" in out


def test_get_quote_multi_symbol_no_depth(app_factory):
    app = app_factory(mode="read_only")
    out = T.get_quote(app, ["005930", "000660"])
    assert len(out["prices"]) == 2
    assert "orderbook" not in out


def test_get_accounts_paper_is_synthetic(app_factory):
    app = app_factory(mode="paper")
    out = T.get_accounts(app)
    assert out["accounts"][0]["accountType"] == "PAPER"


def test_get_accounts_real_in_read_only(app_factory, fake_client):
    app = app_factory(mode="read_only")
    out = T.get_accounts(app)
    assert out["accounts"][0]["accountSeq"] == 7
    assert ("get_accounts",) in fake_client.calls


def test_get_holdings_paper_uses_broker(app_factory):
    app = app_factory(mode="paper")
    app.paper.place(symbol="005930", side="BUY", order_type="LIMIT",
                    fill_price="70000", quantity="2", currency="KRW")
    out = T.get_holdings(app)
    assert out["items"][0]["symbol"] == "005930"
    assert out["cash"]["KRW"] == "9860000"  # 10,000,000 - 140,000


def test_get_holdings_real_in_read_only(app_factory, fake_client):
    app = app_factory(mode="read_only")
    T.get_holdings(app)
    assert ("get_holdings", None) in fake_client.calls


def test_get_market_info_calendar_and_optional_fx(app_factory):
    app = app_factory(mode="read_only")
    out = T.get_market_info(app, "KR", base_currency="USD", quote_currency="KRW")
    assert "calendar" in out and "exchangeRate" in out


def test_get_order_paper_not_found_raises(app_factory):
    app = app_factory(mode="paper")
    import pytest
    with pytest.raises(ValueError):
        T.get_order(app, "nope")
