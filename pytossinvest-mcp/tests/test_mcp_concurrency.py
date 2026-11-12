"""The MCP SDK runs each sync tool in a worker thread, so parallel tool calls hit the same
stores, paper broker and SafetyManager at once."""
import threading
from datetime import date
from decimal import Decimal

import pytest

import pytossinvest_mcp.tools as T
from pytossinvest.errors import ConflictError
from pytossinvest_mcp.paper import MemoryPaperStore, PaperBroker, PaperError
from pytossinvest_mcp.safety import GuardrailError
from pytossinvest_mcp.stores import MemorySpendStore

DAY = date(2026, 6, 17).isoformat()
MEET_TIMEOUT_SEC = 0.2


class _Contended(Decimal):
    """A Decimal whose comparison parks until a second thread reaches the same comparison (or
    a short timeout passes). Used as the cap / cash a check-then-act sequence compares against,
    it puts two unsynchronised threads between the read and the write."""

    meeting = None  # threading.Barrier(2), set per test on the class so copies share it

    def __lt__(self, other):  # reached via `amount > self`
        try:
            self.meeting.wait(timeout=MEET_TIMEOUT_SEC)
        except threading.BrokenBarrierError:
            pass  # the other thread is locked out, which is the behaviour under test
        return Decimal.__lt__(self, other)


def _contended(value):
    _Contended.meeting = threading.Barrier(2)
    return _Contended(value)


def _run_together(*calls):
    """Run the callables in parallel threads; returns each one's result or raised exception."""
    outcomes = [None] * len(calls)

    def run(i, call):
        try:
            outcomes[i] = call()
        except Exception as e:  # noqa: BLE001 - the outcome under test
            outcomes[i] = e

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def test_parallel_reservations_cannot_both_fit_under_the_cap():
    store = MemorySpendStore()
    cap = _contended("1000")

    outcomes = _run_together(
        lambda: store.reserve(DAY, "KRW", Decimal("600"), cap, "order-a"),
        lambda: store.reserve(DAY, "KRW", Decimal("600"), cap, "order-b"),
    )

    assert sorted(outcomes) == [False, True]
    assert store.current(DAY, "KRW") == Decimal("600")


def test_parallel_paper_buys_cannot_spend_the_same_cash():
    broker = PaperBroker(MemoryPaperStore(starting_cash={"KRW": _contended("1000")}))

    def buy():
        return broker.place(symbol="005930", side="BUY", order_type="LIMIT",
                            fill_price="600", quantity="1", currency="KRW")

    outcomes = _run_together(buy, buy)

    assert sum(isinstance(o, PaperError) for o in outcomes) == 1
    assert broker.buying_power("KRW") == Decimal("400")
    assert len(broker.list_orders()) == 1


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_a_token_already_executing_is_refused(app_factory, fake_client, backend):
    # Two calls with one token share one reservation. If the second reached the broker, any
    # refusal it got would release the reservation the first, successful call depends on.
    if backend == "redis":
        pytest.importorskip("fakeredis")
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False, backend=backend)
    real_place = fake_client.place_order
    in_flight, finish = threading.Event(), threading.Event()

    def place(**kwargs):
        if in_flight.is_set():  # what the API answers to a duplicate still being processed
            raise ConflictError("request-in-progress", "", http_status=409)
        in_flight.set()
        assert finish.wait(timeout=5)
        return real_place(**kwargs)

    fake_client.place_order = place
    token = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                            quantity="6", price="100000")["confirmationToken"]
    first = {}
    runner = threading.Thread(
        target=lambda: first.update(result=T.place_order(app, confirmation_token=token)))
    runner.start()
    assert in_flight.wait(timeout=5)

    try:
        with pytest.raises(GuardrailError, match="confirmation-in-progress"):
            T.place_order(app, confirmation_token=token)
    finally:
        finish.set()
        runner.join()

    assert first["result"]["orderId"] == "real-1"
    assert len(fake_client.place_payloads) == 1
    assert app.safety.spend_store.current(DAY, "KRW") == Decimal("600000")


def _place_700k(app):
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="7", price="100000")
    T.place_order(app, confirmation_token=pv["confirmationToken"])


def _fits_600k(app):
    spec = app.safety.build_spec(symbol="005930", side="BUY", order_type="LIMIT",
                                 quantity="6", price="100000")
    return app.safety.reserve(spec)


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_a_downsize_in_flight_frees_no_room_for_other_orders(app_factory, fake_client, backend):
    # A downsize must never lower today's total, least of all before the broker answers: a
    # modify aimed at an order that can no longer change would open room for a parallel order.
    if backend == "redis":
        pytest.importorskip("fakeredis")
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False,
                      daily_order_limit="1000000", backend=backend)
    _place_700k(app)
    in_flight, finish = threading.Event(), threading.Event()

    def refuse_late(order_id, **kwargs):
        in_flight.set()
        assert finish.wait(timeout=5)
        raise ConflictError("already-filled", "", http_status=409)

    fake_client.modify_order = refuse_late
    # real-1 is 70000 x 10 = 700,000 ; down to 10000 x 10 = 100,000 ; delta -600,000
    token = T.preview_modify(app, "real-1", order_type="LIMIT",
                             price="10000")["confirmationToken"]
    outcome = {}

    def modify():
        try:
            T.modify_order(app, confirmation_token=token)
        except ConflictError as e:
            outcome["error"] = e

    runner = threading.Thread(target=modify)
    runner.start()
    assert in_flight.wait(timeout=5)

    try:
        fits_while_in_flight = _fits_600k(app)  # 700,000 + 600,000 > 1,000,000
    finally:
        finish.set()
        runner.join()

    assert fits_while_in_flight is False
    assert outcome["error"].code == "already-filled"
    assert app.safety.spend_store.current(DAY, "KRW") == Decimal("700000")


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_a_confirmed_downsize_refunds_nothing(app_factory, fake_client, backend):
    if backend == "redis":
        pytest.importorskip("fakeredis")
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False,
                      daily_order_limit="1000000", backend=backend)
    _place_700k(app)

    pv = T.preview_modify(app, "real-1", order_type="LIMIT", price="10000")  # 700,000 -> 100,000
    T.modify_order(app, confirmation_token=pv["confirmationToken"])

    assert app.safety.spend_store.current(DAY, "KRW") == Decimal("700000")


def test_a_failed_unclaim_does_not_turn_a_placed_order_into_an_error(app_factory, fake_client):
    # redis-py raises its own ConnectionError, which is not the builtin one
    redis = pytest.importorskip("redis")
    pytest.importorskip("fakeredis")
    app = app_factory(mode="live", allow_live=True, enforce_market_hours=False, backend="redis")
    pv = T.preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                         quantity="1", price="1000")

    def unreachable(token):
        raise redis.exceptions.ConnectionError("Connection reset by peer")
    app.safety.token_store.unclaim = unreachable

    result = T.place_order(app, confirmation_token=pv["confirmationToken"])

    assert result["orderId"] == "real-1"
