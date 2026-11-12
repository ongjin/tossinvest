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


@respx.mock
def test_short_expiry_is_clamped_not_in_past():
    clock = FakeClock()  # t = 1000.0
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok", "token_type": "Bearer", "expires_in": 10})  # 10 < buffer(30)
    )
    mgr, _ = _mgr(clock)
    mgr.get_token()
    # without clamp: 1000 + 10 - 30 = 980 (< issue time). with clamp: 1000 + max(0,-20) = 1000.
    assert mgr._expires_at >= 1000.0


@respx.mock
def test_invalidate_keeps_a_token_newer_than_the_one_that_failed():
    # a 401 on tok-1 arriving after another thread already re-issued must not drop tok-2
    clock = FakeClock()
    route = respx.post(f"{BASE}/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-2", "token_type": "Bearer", "expires_in": 3600}),
        ]
    )
    mgr, _ = _mgr(clock)
    assert mgr.get_token() == "tok-1"
    mgr.invalidate("tok-1")
    assert mgr.get_token() == "tok-2"

    mgr.invalidate("tok-1")  # stale

    assert mgr.get_token() == "tok-2"
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"token": "tok"}),               # no access_token
    httpx.Response(200, json={"access_token": "tok"}),        # no expires_in
    httpx.Response(200, json={"access_token": "tok", "expires_in": "soon"}),
    httpx.Response(200, json=["tok"]),
])
def test_malformed_token_response_raises_oauth_error(response):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=response)
    mgr, _ = _mgr(FakeClock())
    with pytest.raises(OAuthError) as exc:
        mgr.get_token()
    assert exc.value.code == "invalid-response"
