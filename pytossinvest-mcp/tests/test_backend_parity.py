import pytest

import pytossinvest_mcp.tools as T


@pytest.fixture(params=["memory", "redis"])
def backend(request):
    if request.param == "redis":
        pytest.importorskip("fakeredis")
    return request.param


def test_preview_place_parity(app_factory, backend):
    app = app_factory(mode="paper", backend=backend)
    prev = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                           quantity="1", price="70000")
    res = T.place_order(app, confirmation_token=prev["confirmationToken"])
    assert res["status"] == "FILLED"
    assert res["clientOrderId"] == prev["clientOrderId"]


def test_daily_cap_parity(app_factory, backend):
    app = app_factory(mode="paper", backend=backend, daily_order_limit="100000")
    p1 = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="1", price="70000")
    T.place_order(app, confirmation_token=p1["confirmationToken"])
    from pytossinvest_mcp.safety import GuardrailError
    with pytest.raises(GuardrailError, match="daily-limit"):
        T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                        quantity="1", price="70000")  # 140000 > 100000 cap


def test_paper_place_parity(app_factory, backend):
    app = app_factory(mode="paper", backend=backend)
    prev = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                           quantity="1", price="70000")
    res = T.place_order(app, confirmation_token=prev["confirmationToken"])
    assert res["status"] == "FILLED"
    h = T.get_holdings(app)
    assert h["items"][0]["quantity"] == "1"
