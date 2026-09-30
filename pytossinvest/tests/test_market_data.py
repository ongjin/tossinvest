import httpx
import pytest
import respx

from pytossinvest.client import TossInvestClient

BASE = "https://openapi.example.test"
API = f"{BASE}/api/v1"


@pytest.fixture
def client():
    respx.post(f"{BASE}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}))
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)


def _ok(result):
    return httpx.Response(200, json={"result": result})


def _params(route):
    return dict(route.calls[0].request.url.params)


@respx.mock
def test_all_stocks_sends_only_given_filters(client):
    route = respx.get(f"{API}/stocks/all").mock(return_value=_ok([{"symbol": "005930"}]))
    assert client.get_all_stocks("KOSPI", security_type="ETF", common_share=True) == [{"symbol": "005930"}]
    assert _params(route) == {"market": "KOSPI", "securityType": "ETF", "commonShare": "true"}


@pytest.mark.parametrize("method, path", [
    ("get_investor_trading", "investor-trading"),
    ("get_program_trades", "program-trades"),
    ("get_short_selling", "short-selling"),
    ("get_credit_trades", "credit-trades"),
    ("get_securities_lending", "securities-lending"),
])
@respx.mock
def test_trading_trends(client, method, path):
    route = respx.get(f"{API}/stocks/005930/{path}").mock(
        return_value=_ok({"records": [], "nextUntil": None}))
    assert getattr(client, method)("005930", count=5, until="2026-09-01") == {"records": [], "nextUntil": None}
    assert _params(route) == {"count": "5", "until": "2026-09-01"}


@respx.mock
def test_trading_trend_defaults_omit_until(client):
    route = respx.get(f"{API}/stocks/005930/investor-trading").mock(return_value=_ok({"records": []}))
    client.get_investor_trading("005930")
    assert _params(route) == {"count": "10"}


@respx.mock
def test_rankings(client):
    route = respx.get(f"{API}/rankings").mock(return_value=_ok({"rankedAt": "t", "rankings": []}))
    client.get_rankings("TOP_GAINERS", "KR", "1d", exclude_investment_caution=True, count=20)
    assert _params(route) == {"type": "TOP_GAINERS", "marketCountry": "KR", "duration": "1d",
                              "excludeInvestmentCaution": "true", "count": "20"}


@respx.mock
def test_indicator_prices_join_symbols(client):
    route = respx.get(f"{API}/market-indicators/prices").mock(
        return_value=_ok([{"symbol": "KOSPI", "lastPrice": "2600"}]))
    assert client.get_indicator_prices(["KOSPI", "KR_BOND_3Y"])[0]["symbol"] == "KOSPI"
    assert _params(route) == {"symbols": "KOSPI,KR_BOND_3Y"}


@respx.mock
def test_indicator_candles(client):
    route = respx.get(f"{API}/market-indicators/KOSPI/candles").mock(
        return_value=_ok({"candles": [], "nextBefore": None}))
    client.get_indicator_candles("KOSPI", "1d", count=30, before="2026-09-01T00:00:00+09:00")
    assert _params(route) == {"interval": "1d", "count": "30", "before": "2026-09-01T00:00:00+09:00"}


@respx.mock
def test_index_investor_trading(client):
    route = respx.get(f"{API}/market-indicators/KOSDAQ/investor-trading").mock(
        return_value=_ok({"records": []}))
    client.get_index_investor_trading("KOSDAQ", "1w")
    assert _params(route) == {"interval": "1w", "count": "10"}
