from datetime import datetime

from pytossinvest.ratelimit import TokenBucket, effective_rate


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_bucket_starts_full():
    clock = FakeClock()
    b = TokenBucket(capacity=5, refill_per_sec=5, now=clock)
    assert all(b.try_acquire() for _ in range(5))
    assert b.try_acquire() is False


def test_bucket_refills_over_time():
    clock = FakeClock()
    b = TokenBucket(capacity=5, refill_per_sec=5, now=clock)
    for _ in range(5):
        b.try_acquire()
    assert b.try_acquire() is False
    clock.advance(0.2)  # 0.2s * 5/s = 1 token
    assert b.try_acquire() is True


def test_time_until_available():
    clock = FakeClock()
    b = TokenBucket(capacity=1, refill_per_sec=2, now=clock)
    assert b.try_acquire() is True
    # need 1 token at 2/s -> 0.5s
    assert b.time_until_available() == 0.5


def test_capacity_never_exceeded():
    clock = FakeClock()
    b = TokenBucket(capacity=3, refill_per_sec=10, now=clock)
    clock.advance(100)
    granted = sum(1 for _ in range(10) if b.try_acquire())
    assert granted == 3


def test_peak_hour_halves_order_groups():
    peak = datetime(2026, 6, 17, 9, 5)
    off = datetime(2026, 6, 17, 10, 0)
    assert effective_rate("ORDER", 6, peak) == 3
    assert effective_rate("ORDER_INFO", 6, peak) == 3
    assert effective_rate("ORDER", 6, off) == 6
    assert effective_rate("MARKET_DATA", 10, peak) == 10  # not an order group
