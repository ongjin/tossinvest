import httpx
import respx

from pytossinvest.client import TossInvestClient

BASE = "https://openapi.example.test"


def _client():
    return TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None)


@respx.mock
def test_account_call_resolves_account_seq_on_first_use():
    # an MCP client may ask for holdings before ever listing accounts
    respx.post(f"{BASE}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}))
    accounts = respx.get(f"{BASE}/api/v1/accounts").mock(return_value=httpx.Response(
        200, json={"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"}]}))
    holdings = respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json={"result": {"items": []}}))

    c = _client()
    c.get_holdings()
    c.get_holdings()

    assert accounts.call_count == 1  # resolved once, then cached
    assert holdings.calls[0].request.headers["X-Tossinvest-Account"] == "7"


def _token_and_holdings():
    respx.post(f"{BASE}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}))
    accounts = respx.get(f"{BASE}/api/v1/accounts").mock(return_value=httpx.Response(
        200, json={"result": [{"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"},
                              {"accountNo": "2", "accountSeq": 42, "accountType": "PENSION"}]}))
    holdings = respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json={"result": {"items": []}}))
    return accounts, holdings


@respx.mock
def test_explicit_account_seq_is_used_without_listing_accounts():
    accounts, holdings = _token_and_holdings()
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, account_seq=42)

    c.get_holdings()

    assert holdings.calls.last.request.headers["X-Tossinvest-Account"] == "42"
    assert accounts.call_count == 0


@respx.mock
def test_listing_accounts_does_not_replace_an_explicit_account_seq():
    _, holdings = _token_and_holdings()
    c = TossInvestClient("cid", "secret", base_url=BASE, sleep=lambda s: None, account_seq=42)

    c.get_accounts()  # first account is 7
    c.get_holdings()

    assert holdings.calls.last.request.headers["X-Tossinvest-Account"] == "42"
