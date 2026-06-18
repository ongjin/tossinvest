from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable

__all__ = ["TokenBucket", "effective_rate", "backoff_wait", "PEAK_GROUPS"]

PEAK_GROUPS = {"ORDER", "ORDER_INFO"}
_PEAK_START = time(9, 0)
_PEAK_END = time(9, 10)


@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    now: Callable[[], float]
    _tokens: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last = self.now()

    def _refill(self) -> None:
        t = self.now()
        elapsed = t - self._last
        self._last = t
        self._tokens = min(
            self.capacity, self._tokens + elapsed * self.refill_per_sec
        )

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def time_until_available(self, n: float = 1.0) -> float:
        self._refill()
        if self._tokens >= n:
            return 0.0
        return (n - self._tokens) / self.refill_per_sec


def effective_rate(group: str, base_rate: float, now_kst: datetime) -> float:
    """Order groups are halved during the 09:00-09:10 KST open auction window."""
    if group in PEAK_GROUPS and _PEAK_START <= now_kst.time() < _PEAK_END:
        return base_rate / 2
    return base_rate


def backoff_wait(
    attempt: int,
    retry_after: "float | None",
    *,
    base: float = 1.0,
    cap: float,
    rng: Callable[[], float],
) -> float:
    """Seconds to wait before a 429 retry. Honors Retry-After (>0); else exponential
    backoff (base * 2**attempt) with full jitter. Clamped to cap to avoid unbounded waits."""
    if retry_after is not None and retry_after > 0:
        wait = retry_after
    else:
        wait = base * (2 ** attempt) * rng()
    return min(wait, cap)
