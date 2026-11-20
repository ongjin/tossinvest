import runpy
from pathlib import Path

import pytest
import respx

BASE = "https://openapi.tossinvest.com"
QUICKSTART = Path(__file__).with_name("sdk_quickstart.py")


@pytest.mark.parametrize("quotes", [[], [{"symbol": "005930", "lastPrice": "70000", "currency": "KRW"}]])
@respx.mock
def test_quickstart_with_credentials_only_reads(monkeypatch, quotes):
    monkeypatch.setenv("TOSSINVEST_CLIENT_ID", "cid")
    monkeypatch.setenv("TOSSINVEST_CLIENT_SECRET", "secret")
    respx.post(f"{BASE}/oauth2/token").respond(json={
        "access_token": "tok", "token_type": "Bearer", "expires_in": 3600})
    accounts = respx.get(f"{BASE}/api/v1/accounts").respond(json={"result": [
        {"accountNo": "1", "accountSeq": 1, "accountType": "BROKERAGE"}]})
    prices = respx.get(f"{BASE}/api/v1/prices").respond(json={"result": quotes})

    runpy.run_path(str(QUICKSTART), run_name="__main__")
    assert accounts.called and prices.called
