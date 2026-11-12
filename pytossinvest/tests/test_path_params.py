"""Ids and symbols are interpolated into URL paths. A caller-supplied value (an LLM's, behind
the MCP server) must stay one path segment and never pick a different endpoint."""
import httpx
import pytest
import respx

from pytossinvest.client import TossInvestClient

BASE = "https://openapi.example.test"

CONDITIONAL = dict(type="SINGLE", quantity="1", order_type="LIMIT", expire_date="2026-12-31",
                   first={"orderSide": "SELL", "triggerPrice": "1"})


def _client():
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)
    c._account_seq = 1
    return c


def _mock_api():
    respx.post(f"{BASE}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 3600}))
    return respx.route(path__startswith="/api/").mock(
        return_value=httpx.Response(200, json={"result": {}}))


@respx.mock
@pytest.mark.parametrize("call", [
    lambda c: c.get_order("../holdings"),
    lambda c: c.cancel_order("A/modify#"),
    lambda c: c.modify_order("A?x=1", order_type="LIMIT"),
    lambda c: c.get_conditional_order("..%2Forders"),          # pre-encoded slash
    lambda c: c.modify_conditional_order("7\\..\\8", **CONDITIONAL),  # backslash
    lambda c: c.cancel_conditional_order("9;cancel"),          # path parameter
    lambda c: c.get_investor_trading("../../holdings?"),
    lambda c: c.get_indicator_candles("KOSPI x", "1d"),        # whitespace
    lambda c: c.get_index_investor_trading("KOSPI\n", "1d"),   # control character
    lambda c: c.get_order("A#fragment"),                       # fragment alone
    lambda c: c.get_order("A\u200bB"),                         # zero-width, not printable
    lambda c: c.get_market_calendar("KR/../../holdings"),
])
def test_value_that_could_restructure_the_path_is_refused(call):
    # Refused rather than percent-encoded: whether a server or proxy decodes %2F and
    # normalises the path again is not something the client can know.
    route = _mock_api()

    with pytest.raises(ValueError):
        call(_client())

    assert route.call_count == 0


@respx.mock
@pytest.mark.parametrize("call, path", [
    (lambda c: c.get_short_selling("BRK.B"), "/api/v1/stocks/BRK.B/short-selling?count=10"),
    (lambda c: c.get_order("ord-1_A.x~9"), "/api/v1/orders/ord-1_A.x~9"),
    (lambda c: c.get_indicator_candles("KR_BOND_2Y", "1d"),
     "/api/v1/market-indicators/KR_BOND_2Y/candles?interval=1d&count=100"),
])
def test_ordinary_values_are_sent_unchanged(call, path):
    route = _mock_api()

    call(_client())

    assert route.calls.last.request.url.raw_path.decode() == path


@respx.mock
def test_other_reserved_characters_are_percent_encoded():
    route = _mock_api()

    _client().get_order("a:b=c+d@e&f")

    assert route.calls.last.request.url.raw_path.decode() == "/api/v1/orders/a%3Ab%3Dc%2Bd%40e%26f"


@respx.mock
@pytest.mark.parametrize("order_id", ["", ".", ".."])
def test_dot_or_empty_segment_is_refused_before_any_request(order_id):
    # "." and ".." are resolved as path navigation and "" collapses /orders/{id} into the
    # list endpoint
    route = _mock_api()

    with pytest.raises(ValueError):
        _client().cancel_order(order_id)

    assert route.call_count == 0
