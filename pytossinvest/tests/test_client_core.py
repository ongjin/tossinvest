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
def test_retries_once_on_revoked_token():
    # the API keeps one live token per client: another process re-issuing revokes ours
    token_route = _token_route()
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"code": "token-revoked", "message": ""}}),
            httpx.Response(200, json={"result": []}),
        ]
    )
    c = _client()
    result = c._request("GET", "/api/v1/accounts", group="ACCOUNT")
    assert result == []
    assert route.call_count == 2
    assert token_route.call_count == 2  # the revoked token was dropped and re-issued


@respx.mock
def test_max_retries_zero_raises_immediately():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "2"}))
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, max_retries=0)
    with pytest.raises(RateLimitError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert route.call_count == 1                 # no retry
    assert exc.value.retry_after == 2.0


@respx.mock
def test_429_then_200_retries_to_success():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(side_effect=[
        httpx.Response(429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"result": []}),
    ])
    c = _client()                                # default max_retries=3, sleep no-op
    result = c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert result == []
    assert route.call_count == 2


@respx.mock
def test_429_honors_retry_after_in_sleep():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(side_effect=[
        httpx.Response(429, json={"error": {}}, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"result": []}),
    ])
    slept = []
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: slept.append(s))
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert slept == [2.0]


@respx.mock
def test_429_backoff_jitter_when_no_retry_after():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(side_effect=[
        httpx.Response(429, json={"error": {}}),    # no Retry-After
        httpx.Response(200, json={"result": []}),
    ])
    slept = []
    c = TossInvestClient("cid", "secret", base_url=BASE,
                         sleep=lambda s: slept.append(s), rng=lambda: 0.5)
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert slept == [0.5]                         # 1 * 2**0 * 0.5


@respx.mock
def test_429_exhausts_retries_then_raises():
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        429, json={"error": {"code": "rate-limit-exceeded"}}, headers={"Retry-After": "1"}))
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, max_retries=2)
    with pytest.raises(RateLimitError):
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert route.call_count == 3                  # 1 initial + 2 retries


@respx.mock
def test_5xx_is_not_retried():
    from pytossinvest.errors import ServerError
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        503, json={"error": {"code": "server-error"}}))
    c = _client()                                # default max_retries=3
    with pytest.raises(ServerError):
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert route.call_count == 1                  # no retry on 5xx


@respx.mock
def test_429_retry_preserves_client_order_id_on_post():
    import json as _json
    _token_route()
    route = respx.post(f"{BASE}/api/v1/orders").mock(side_effect=[
        httpx.Response(429, json={"error": {}}, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"result": {"orderId": "1", "clientOrderId": "abc"}}),
    ])
    c = _client()
    c._account_seq = 1
    c.place_order(symbol="005930", side="BUY", order_type="LIMIT",
                  quantity="1", price="100", client_order_id="abc")
    assert route.call_count == 2
    for call in route.calls:
        assert _json.loads(call.request.content)["clientOrderId"] == "abc"  # idempotent retry


def test_gate_applies_peak_hour_halving():
    from datetime import datetime

    peak = datetime(2026, 6, 17, 9, 5)  # inside 09:00-09:10 KST window
    off = datetime(2026, 6, 17, 11, 0)

    c_peak = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, now_kst=lambda: peak)
    c_peak._gate("ORDER")
    assert c_peak._buckets["ORDER"].capacity == 3  # 6 -> 3 during peak
    c_peak._gate("MARKET_DATA")
    assert c_peak._buckets["MARKET_DATA"].capacity == 10  # non-order group unaffected

    c_off = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, now_kst=lambda: off)
    c_off._gate("ORDER")
    assert c_off._buckets["ORDER"].capacity == 6  # full rate outside window


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


@respx.mock
def test_200_non_json_body_raises_invalid_response():
    from pytossinvest.errors import TossInvestError
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(200, text="not json")
    )
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert exc.value.code == "invalid-response"


@respx.mock
def test_200_missing_result_raises_missing_result():
    from pytossinvest.errors import TossInvestError
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(200, json={"data": [1, 2, 3]})  # no "result" key
    )
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    assert exc.value.code == "missing-result"


@respx.mock
def test_200_deeply_nested_json_raises_invalid_response():
    from pytossinvest.errors import TossInvestError
    _token_route()
    deep = b"[" * 50000 + b"]" * 50000
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(200, content=deep))
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    # Python 3.12's json parser gives up (RecursionError); 3.14 parses it into a non-object.
    # Either way it must be a typed error, never a crash.
    assert exc.value.code in {"invalid-response", "missing-result"}


@respx.mock
def test_syncs_bucket_from_ratelimit_headers():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        200, json={"result": []},
        headers={"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "2"},
    ))
    c = _client()
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    b = c._buckets["MARKET_DATA"]
    assert b.capacity == 20.0
    assert b.refill_per_sec == 0.5          # 1 / Reset(2)
    assert b._tokens <= 5.0                  # clamped down to Remaining


@respx.mock
def test_no_ratelimit_headers_keeps_static_rate():
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(200, json={"result": []}))
    c = _client()
    c._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": "005930"})
    c._gate("MARKET_DATA")                   # no header seen -> static default applies
    assert c._buckets["MARKET_DATA"].capacity == 10


@respx.mock
def test_header_rate_overrides_peak_halving():
    from datetime import datetime
    _token_route()
    respx.post(f"{BASE}/api/v1/orders").mock(return_value=httpx.Response(
        200, json={"result": {"orderId": "1"}},
        headers={"X-RateLimit-Limit": "6", "X-RateLimit-Remaining": "6", "X-RateLimit-Reset": "1"},
    ))
    peak = datetime(2026, 6, 17, 9, 5)       # inside 09:00-09:10 peak window
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, now_kst=lambda: peak)
    c._account_seq = 1
    c._request("POST", "/api/v1/orders", group="ORDER", account=True, json={})
    c._gate("ORDER")                         # header seen -> must NOT re-halve 6 to 3
    assert c._buckets["ORDER"].capacity == 6.0


@respx.mock
def test_401_with_non_standard_body_raises_auth_error():
    from pytossinvest.errors import AuthError
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"}))  # edge/gateway shape
    c = _client()
    with pytest.raises(AuthError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA")
    assert exc.value.code == "unknown"
    assert route.call_count == 1  # not a token problem we recognise, so no re-issue


@respx.mock
@pytest.mark.parametrize("content", [b"null", b"7", b'"result"', b'["result"]'])
def test_200_non_object_body_raises_missing_result(content):
    from pytossinvest.errors import TossInvestError
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(200, content=content))
    c = _client()
    with pytest.raises(TossInvestError) as exc:
        c._request("GET", "/api/v1/prices", group="MARKET_DATA")
    assert exc.value.code == "missing-result"


class _SteppingClock:
    """monotonic + sleep pair: sleeping advances time, and a runaway gate loop is cut short."""

    MAX_SLEEPS = 50

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        assert len(self.slept) <= self.MAX_SLEEPS, "rate gate never opened"
        self.t += seconds


def _client_on(clock, **kwargs):
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=clock.sleep,
                            monotonic=clock.now, **kwargs)


@respx.mock
@pytest.mark.parametrize("limit, remaining, reset", [
    ("0", "0", "1"),      # capacity 0: a token could never be acquired
    ("nan", "5", "1"),
    ("10", "0", "inf"),   # refill rate 0
    ("-3", "0", "1"),
])
def test_nonsense_ratelimit_headers_are_ignored(limit, remaining, reset):
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        200, json={"result": []},
        headers={"X-RateLimit-Limit": limit, "X-RateLimit-Remaining": remaining,
                 "X-RateLimit-Reset": reset},
    ))
    clock = _SteppingClock()
    c = _client_on(clock)
    c._request("GET", "/api/v1/prices", group="MARKET_DATA")

    c._gate("MARKET_DATA")  # must return: the documented default (10/s) still applies

    assert c._buckets["MARKET_DATA"].capacity == 10
    assert clock.slept == []


@respx.mock
def test_gate_wait_is_bounded_by_retry_max_wait():
    # a server-reported refill period far beyond retry_max_wait must not park the caller:
    # past the cap we send anyway and let the bounded 429 retry path decide
    _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(
        200, json={"result": []},
        headers={"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "0",
                 "X-RateLimit-Reset": "3600"},
    ))
    clock = _SteppingClock()
    c = _client_on(clock, retry_max_wait=60.0)
    c._request("GET", "/api/v1/prices", group="MARKET_DATA")

    c._gate("MARKET_DATA")

    assert 0 < sum(clock.slept) <= 60.0


def _revoked():
    return httpx.Response(401, json={"error": {"code": "token-revoked", "message": ""}})


@respx.mock
def test_recovers_when_the_reissued_token_is_revoked_again():
    # a second process on the same client id can issue between our re-issue and our retry
    token_route = _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(side_effect=[
        _revoked(), _revoked(), httpx.Response(200, json={"result": []})])
    slept = []
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=slept.append, rng=lambda: 0.5)

    assert c._request("GET", "/api/v1/prices", group="MARKET_DATA") == []

    assert route.call_count == 3
    assert token_route.call_count == 3
    assert slept == [0.25]  # one jittered pause, before the second re-issue only


@respx.mock
def test_token_reissue_is_bounded():
    from pytossinvest.errors import AuthError
    _token_route()
    route = respx.get(f"{BASE}/api/v1/prices").mock(return_value=_revoked())
    c = _client()

    with pytest.raises(AuthError):
        c._request("GET", "/api/v1/prices", group="MARKET_DATA")

    assert route.call_count == 3  # the first send and two re-issues


@respx.mock
def test_clients_sharing_a_token_cache_issue_one_token():
    from test_auth import SharedCache
    token_route = _token_route()
    respx.get(f"{BASE}/api/v1/prices").mock(return_value=httpx.Response(200, json={"result": []}))
    cache = SharedCache()
    first = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, token_cache=cache)
    second = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, token_cache=cache)

    first._request("GET", "/api/v1/prices", group="MARKET_DATA")
    second._request("GET", "/api/v1/prices", group="MARKET_DATA")

    assert token_route.call_count == 1
