"""One client shared by several threads (an MCP server runs each sync tool in a worker thread)."""
import threading
import time

import httpx
import respx

from pytossinvest.client import TossInvestClient
from pytossinvest.errors import TossInvestError

BASE = "https://openapi.example.test"
LATENCY_SEC = 0.05  # long enough that every thread is in flight before the first response lands
THREADS = 4


class OneLiveTokenServer:
    """Mimics the API's token policy: issuing a token revokes the previous one."""

    def __init__(self):
        self._lock = threading.Lock()
        self._issued = 0
        self.live = None

    def token(self, request):
        time.sleep(LATENCY_SEC)
        with self._lock:
            self._issued += 1
            self.live = f"tok-{self._issued}"
            return httpx.Response(200, json={"access_token": self.live, "expires_in": 3600})

    def prices(self, request):
        time.sleep(LATENCY_SEC)
        sent = request.headers["Authorization"].removeprefix("Bearer ")
        if sent != self.live:
            return httpx.Response(401, json={"error": {"code": "token-revoked", "message": ""}})
        return httpx.Response(200, json={"result": []})


def _client(**kwargs):
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, **kwargs)


def _run_in_threads(fn, count=THREADS):
    """Run fn in `count` threads released together; returns the SDK errors they raised."""
    errors = []
    barrier = threading.Barrier(count)

    def work():
        barrier.wait()
        try:
            fn()
        except TossInvestError as e:
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


@respx.mock
def test_parallel_cold_start_issues_one_token():
    server = OneLiveTokenServer()
    token_route = respx.post(f"{BASE}/oauth2/token").mock(side_effect=server.token)
    respx.get(f"{BASE}/api/v1/prices").mock(side_effect=server.prices)
    c = _client()

    errors = _run_in_threads(lambda: c.get_prices(["005930"]))

    assert errors == []
    assert token_route.call_count == 1


@respx.mock
def test_parallel_calls_recover_together_from_a_revoked_token():
    server = OneLiveTokenServer()
    token_route = respx.post(f"{BASE}/oauth2/token").mock(side_effect=server.token)
    respx.get(f"{BASE}/api/v1/prices").mock(side_effect=server.prices)
    c = _client()
    c.get_prices(["005930"])  # caches tok-1
    server.live = "issued-by-another-process"  # revokes tok-1 behind our back

    errors = _run_in_threads(lambda: c.get_prices(["005930"]))

    assert errors == []
    assert token_route.call_count == 2  # one re-issue shared by every thread


@respx.mock
def test_parallel_account_calls_list_accounts_once():
    respx.post(f"{BASE}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 3600}))

    def accounts(request):
        time.sleep(LATENCY_SEC)
        return httpx.Response(200, json={"result": [
            {"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]})

    accounts_route = respx.get(f"{BASE}/api/v1/accounts").mock(side_effect=accounts)
    respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json={"result": {"items": []}}))
    c = _client()

    errors = _run_in_threads(c.get_holdings)

    assert errors == []
    assert accounts_route.call_count == 1  # ACCOUNT is 1 req/s: a stampede would queue for seconds


def test_header_sync_during_gate_is_not_overwritten_by_static_rate():
    # Thread A is inside the gate applying the documented default while thread B's response
    # delivers the server's real limit. The header value must be the one that survives.
    from datetime import datetime

    in_gate = threading.Event()
    synced = threading.Event()

    def now_kst():
        in_gate.set()
        synced.wait(timeout=0.2)
        return datetime(2026, 6, 17, 11, 0)

    c = _client(now_kst=now_kst)
    headers = {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "20", "X-RateLimit-Reset": "1"}

    def sync():
        in_gate.wait()
        c._sync_bucket_from_headers("MARKET_DATA", headers)
        synced.set()

    syncer = threading.Thread(target=sync)
    syncer.start()
    c._gate("MARKET_DATA")
    syncer.join()

    assert c._buckets["MARKET_DATA"].capacity == 20.0
