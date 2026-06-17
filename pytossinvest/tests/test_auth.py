import httpx
import respx

from pytossinvest.auth import TokenManager
from pytossinvest.errors import OAuthError
import pytest

BASE = "https://openapi.tossinvest.com"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _mgr(clock):
    http = httpx.Client(base_url=BASE)
    return TokenManager("cid", "secret", http=http, now=clock), http


@respx.mock
def test_fetches_and_caches_token():
    clock = FakeClock()
    route = respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600})
    )
    mgr, _ = _mgr(clock)
    assert mgr.get_token() == "tok-1"
    assert mgr.get_token() == "tok-1"  # cached
    assert route.call_count == 1


@respx.mock
def test_refreshes_after_expiry():
    clock = FakeClock()
    respx.post(f"{BASE}/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 100}),
            httpx.Response(200, json={"access_token": "tok-2", "token_type": "Bearer", "expires_in": 100}),
        ]
    )
    mgr, _ = _mgr(clock)
    assert mgr.get_token() == "tok-1"
    clock.advance(200)  # well past expiry
    assert mgr.get_token() == "tok-2"


@respx.mock
def test_invalidate_forces_refetch():
    clock = FakeClock()
    respx.post(f"{BASE}/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-2", "token_type": "Bearer", "expires_in": 3600}),
        ]
    )
    mgr, _ = _mgr(clock)
    assert mgr.get_token() == "tok-1"
    mgr.invalidate()
    assert mgr.get_token() == "tok-2"


@respx.mock
def test_oauth_failure_raises_oauth_error():
    clock = FakeClock()
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_client", "error_description": "bad"})
    )
    mgr, _ = _mgr(clock)
    with pytest.raises(OAuthError) as exc:
        mgr.get_token()
    assert exc.value.code == "invalid_client"


@respx.mock
def test_non_json_error_body_still_raises_oauth_error():
    clock = FakeClock()
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )
    mgr, _ = _mgr(clock)
    with pytest.raises(OAuthError) as exc:
        mgr.get_token()
    assert exc.value.http_status == 502
