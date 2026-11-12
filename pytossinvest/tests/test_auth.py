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


# --- a token cache shared between processes ---

import contextlib


class SharedCache:
    """In-memory stand-in for a cache two processes share (e.g. one redis)."""

    def __init__(self):
        self.entry = None
        self.locked = False

    @contextlib.contextmanager
    def lock(self):
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def load(self):
        return self.entry

    def save(self, token, ttl_sec):
        self.entry = (token, ttl_sec)

    def discard(self, token):
        if self.entry is not None and self.entry[0] == token:
            self.entry = None


def _token_responses(*tokens, expires_in=3600):
    return [httpx.Response(200, json={"access_token": t, "token_type": "Bearer",
                                      "expires_in": expires_in}) for t in tokens]


def _process(cache, clock=None):
    """A TokenManager as another process would build it: own state, same cache."""
    http = httpx.Client(base_url=BASE)
    return TokenManager("cid", "secret", http=http, now=clock or FakeClock(), cache=cache)


@respx.mock
def test_second_process_adopts_the_shared_token_instead_of_issuing():
    # issuing would revoke the token the first process is using
    route = respx.post(f"{BASE}/oauth2/token").mock(side_effect=_token_responses("tok-1", "tok-2"))
    cache = SharedCache()

    assert _process(cache).get_token() == "tok-1"
    assert _process(cache).get_token() == "tok-1"

    assert route.call_count == 1


@respx.mock
def test_adopted_token_expires_with_the_life_it_had_left():
    respx.post(f"{BASE}/oauth2/token").mock(side_effect=_token_responses("tok-2"))
    cache = SharedCache()
    cache.entry = ("tok-1", 50.0)
    clock = FakeClock()
    mgr = _process(cache, clock)
    assert mgr.get_token() == "tok-1"

    clock.advance(60)
    cache.entry = None  # a real cache has expired the entry by now

    assert mgr.get_token() == "tok-2"


@respx.mock
def test_token_is_issued_under_the_cache_lock():
    cache = SharedCache()

    def issue(request):
        assert cache.locked, "another process could issue at the same time"
        return _token_responses("tok-1")[0]
    respx.post(f"{BASE}/oauth2/token").mock(side_effect=issue)

    assert _process(cache).get_token() == "tok-1"
    assert cache.entry[0] == "tok-1"


@respx.mock
def test_revoked_shared_token_is_reissued_once_for_every_process():
    route = respx.post(f"{BASE}/oauth2/token").mock(side_effect=_token_responses("tok-1", "tok-2", "tok-3"))
    cache = SharedCache()
    first, second = _process(cache), _process(cache)
    assert first.get_token() == second.get_token() == "tok-1"

    first.invalidate("tok-1")   # both got 401 token-revoked for tok-1
    assert cache.entry is None  # so no other process spends a call finding out it is dead
    assert first.get_token() == "tok-2"
    second.invalidate("tok-1")  # late: must not drop the tok-2 the first one just shared
    assert second.get_token() == "tok-2"

    assert route.call_count == 2


@respx.mock
def test_token_too_short_lived_to_use_is_not_shared():
    respx.post(f"{BASE}/oauth2/token").mock(side_effect=_token_responses("tok-1", expires_in=10))
    cache = SharedCache()

    _process(cache).get_token()  # 10s is inside the expiry buffer: unusable for anyone else

    assert cache.entry is None


@respx.mock
def test_a_token_that_failed_is_not_adopted_again_from_a_cache_that_kept_it():
    # e.g. the cache could not be written to: without this, every call would re-adopt the
    # revoked token until the cache entry expired on its own
    respx.post(f"{BASE}/oauth2/token").mock(side_effect=_token_responses("tok-2"))
    cache = SharedCache()
    cache.entry = ("tok-1", 3600.0)
    cache.discard = lambda token: None  # the write is lost
    mgr = _process(cache)
    assert mgr.get_token() == "tok-1"

    mgr.invalidate("tok-1")

    assert mgr.get_token() == "tok-2"


@respx.mock
def test_a_dead_token_stuck_in_the_cache_is_never_adopted_again():
    # the cache can be read but not written: it keeps handing out the same dead token
    respx.post(f"{BASE}/oauth2/token").mock(side_effect=_token_responses("tok-2", "tok-3"))
    cache = SharedCache()
    cache.entry = ("dead", 3600.0)
    cache.discard = lambda token: None
    cache.save = lambda token, ttl_sec: None
    mgr = _process(cache)

    assert mgr.get_token() == "dead"
    mgr.invalidate("dead")
    assert mgr.get_token() == "tok-2"
    mgr.invalidate("tok-2")  # revoked by another process doing the same

    assert mgr.get_token() == "tok-3"
