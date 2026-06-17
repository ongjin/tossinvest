import httpx
import respx
import pytest

from pytossinvest.client import TossInvestClient
from pytossinvest.errors import ValidationError, RateLimitError

BASE = "https://openapi.tossinvest.com"


def _token_route():
    return respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
    )


def _client():
    # sleep is a no-op so rate-limit waits don't slow tests
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)


@respx.mock
def test_unwraps_result_and_sends_auth():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]})
    )
    c = _client()
    result = c._request("GET", "/api/v1/accounts", group="ACCOUNT")
    assert result == [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_account_header_added_when_requested():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json={"result": {"items": []}})
    )
    c = _client()
    c._account_seq = 3  # pretend cached
    c._request("GET", "/api/v1/holdings", group="ASSET", account=True)
    assert route.calls.last.request.headers["X-Tossinvest-Account"] == "3"


@respx.mock
def test_maps_error_by_status():
    _token_route()
    respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(400, json={"error": {"code": "account-header-required", "message": ""}})
    )
    c = _client()
    with pytest.raises(ValidationError) as exc:
        c._request("GET", "/api/v1/holdings", group="ASSET")
    assert exc.value.code == "account-header-required"


@respx.mock
def test_retries_once_on_expired_token():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"code": "expired-token", "message": ""}}),
            httpx.Response(200, json={"result": []}),
        ]
    )
    c = _client()
    result = c._request("GET", "/api/v1/accounts", group="ACCOUNT")
    assert result == []
    assert route.call_count == 2


@respx.mock
def test_429_raises_rate_limit_error():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "2"})
    )
    c = _client()
    with pytest.raises(RateLimitError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert exc.value.retry_after == 2.0


@respx.mock
def test_non_json_error_body_raises_typed_error():
    from pytossinvest.errors import TossInvestError
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
    )
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA")
    assert exc.value.http_status == 502
