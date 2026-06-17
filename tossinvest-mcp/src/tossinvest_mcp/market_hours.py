from __future__ import annotations

from datetime import datetime, time


def _parse_hhmm(value: str) -> time:
    parts = value.split(":")
    return time(int(parts[0]), int(parts[1]))


def is_market_open(calendar: "dict | None", now_kst: datetime, country: str) -> bool:
    """Best-effort: open iff `now_kst` falls in today's regular-market session.

    Tolerates missing/closed/unknown shapes by returning False. The API gives all
    times in KST. A US session stated in KST wraps past midnight (e.g. 23:30->06:00);
    when startTime > endTime the window is treated as [start, 24:00) ∪ [00:00, end).
    The v1 hours gate runs only in live mode and can be overridden via
    enforce_market_hours=False.
    """
    today = (calendar or {}).get("today") or {}
    if country.upper() == "KR":
        session = (today.get("integrated") or {}).get("regularMarket") or {}
    else:
        session = today.get("regularMarket") or {}
    start, end = session.get("startTime"), session.get("endTime")
    if not start or not end:
        return False
    start_t, end_t, now_t = _parse_hhmm(start), _parse_hhmm(end), now_kst.time()
    if start_t <= end_t:
        return start_t <= now_t < end_t
    # wraps past midnight (US session stated in KST)
    return now_t >= start_t or now_t < end_t
