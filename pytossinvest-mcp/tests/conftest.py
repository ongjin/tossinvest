from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from pytossinvest.models import Account, BuyingPower, Price
from pytossinvest_mcp.audit import AuditLog
from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.paper import PaperBroker
from pytossinvest_mcp.safety import SafetyManager
from pytossinvest_mcp.tools import AppContext

KST = ZoneInfo("Asia/Seoul")


class FakeClient:
    """Mimics the subset of TossInvestClient that the MCP tools call. Records calls."""

    def __init__(self):
        self.calls = []
        self.place_payloads = []

    # market data
    def get_prices(self, symbols):
        self.calls.append(("get_prices", symbols))
        return [Price.model_validate({"symbol": s, "lastPrice": "70000", "currency": "KRW"}) for s in symbols]

    def get_orderbook(self, symbol):
        self.calls.append(("get_orderbook", symbol))
        return {"symbol": symbol, "asks": [], "bids": []}

    def get_trades(self, symbol, count=50):
        self.calls.append(("get_trades", symbol))
        return [{"price": "70000", "volume": "1"}]

    def get_candles(self, symbol, interval, count=100, before=None):
        self.calls.append(("get_candles", symbol, interval))
        return {"candles": [], "nextBefore": None}

    def get_stocks(self, symbols):
        self.calls.append(("get_stocks", symbols))
        return [{"symbol": s, "name": "삼성전자"} for s in symbols]

    def get_exchange_rate(self, base, quote):
        self.calls.append(("get_exchange_rate", base, quote))
        return {"baseCurrency": base, "quoteCurrency": quote, "rate": "1350"}

    def get_market_calendar(self, country, date=None):
        self.calls.append(("get_market_calendar", country))
        return {"today": {"integrated": {"regularMarket": {"startTime": "09:00", "endTime": "15:30"}}}}

    # account / order (real-mode path)
    def get_accounts(self):
        self.calls.append(("get_accounts",))
        return [Account.model_validate({"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"})]

    def get_holdings(self, symbol=None):
        self.calls.append(("get_holdings", symbol))
        return {"items": [], "marketValue": {"krw": "0"}}

    def get_buying_power(self, currency):
        self.calls.append(("get_buying_power", currency))
        return BuyingPower.model_validate({"currency": currency, "cashBuyingPower": "500000"})

    def get_sellable_quantity(self, symbol):
        self.calls.append(("get_sellable_quantity", symbol))
        return {"sellableQuantity": "0"}

    def get_commissions(self):
        self.calls.append(("get_commissions",))
        return [{"marketCountry": "KR", "commissionRate": "0.015"}]

    def list_orders(self, status="OPEN", symbol=None, cursor=None, limit=20):
        self.calls.append(("list_orders", status, symbol))
        return {"orders": [], "hasNext": False}

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return {"orderId": order_id, "symbol": "005930", "side": "BUY",
                "orderType": "LIMIT", "quantity": "10", "price": "70000", "status": "PENDING"}

    def place_order(self, **kwargs):
        from pytossinvest.models import OrderResponse
        self.place_payloads.append(kwargs)
        return OrderResponse.model_validate({"orderId": "real-1", "clientOrderId": kwargs.get("client_order_id")})

    def modify_order(self, order_id, **kwargs):
        self.calls.append(("modify_order", order_id, kwargs))
        return {"orderId": "real-2"}

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        return {"orderId": "real-3"}


@pytest.fixture
def fake_client():
    return FakeClient()


def _make_stores(backend):
    if backend == "redis":
        import fakeredis
        from pytossinvest_mcp.redis_stores import RedisTokenStore, RedisSpendStore, RedisPaperStore
        r = fakeredis.FakeStrictRedis(decode_responses=True)
        return (RedisTokenStore(r), RedisSpendStore(r),
                RedisPaperStore(r, starting_cash={"KRW": "10000000", "USD": "1000000"}))
    from pytossinvest_mcp.stores import MemoryTokenStore, MemorySpendStore
    from pytossinvest_mcp.paper import MemoryPaperStore
    return (MemoryTokenStore(), MemorySpendStore(),
            MemoryPaperStore(starting_cash={"KRW": "10000000", "USD": "1000000"}))


def make_app(fake_client, tmp_path, *, mode="paper", backend="memory", now_kst=None, **settings_kw):
    settings = Settings(_env_file=None, mode=mode,
                        audit_log_path=str(tmp_path / "audit.log"), **settings_kw)
    token_store, spend_store, paper_store = _make_stores(backend)
    paper = PaperBroker(paper_store, next_id=_counter("paper"))
    safety = SafetyManager(settings, now=lambda: 1000.0, today=lambda: date(2026, 6, 17),
                           gen_id=_counter("cli"),
                           token_store=token_store, spend_store=spend_store)
    audit = AuditLog(settings.audit_log_path)
    return AppContext(
        config=settings, client=fake_client, paper=paper, safety=safety, audit=audit,
        now_kst=now_kst or (lambda: datetime(2026, 6, 17, 10, 0, tzinfo=KST)),
    )


def _counter(prefix):
    state = {"i": 0}
    def gen():
        state["i"] += 1
        return f"{prefix}-{state['i']}"
    return gen


@pytest.fixture
def app_factory(fake_client, tmp_path):
    def factory(**kw):
        return make_app(fake_client, tmp_path, **kw)
    return factory
