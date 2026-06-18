from datetime import datetime
from zoneinfo import ZoneInfo

from pytossinvest_mcp.market_hours import is_market_open

KST = ZoneInfo("Asia/Seoul")

KR_OPEN_DAY = {"today": {"integrated": {"regularMarket": {"startTime": "09:00", "endTime": "15:30"}}}}
KR_HOLIDAY = {"today": {"integrated": {}}}
US_OPEN_DAY = {"today": {"regularMarket": {"startTime": "23:30", "endTime": "06:00"}}}


def _kst(h, m):
    return datetime(2026, 6, 17, h, m, tzinfo=KST)


def test_kr_inside_session_is_open():
    assert is_market_open(KR_OPEN_DAY, _kst(10, 0), "KR") is True


def test_kr_before_open_is_closed():
    assert is_market_open(KR_OPEN_DAY, _kst(8, 59), "KR") is False


def test_kr_at_close_is_closed():
    # end is exclusive
    assert is_market_open(KR_OPEN_DAY, _kst(15, 30), "KR") is False


def test_kr_holiday_is_closed():
    assert is_market_open(KR_HOLIDAY, _kst(10, 0), "KR") is False


def test_us_session_read_from_regular_market():
    assert is_market_open(US_OPEN_DAY, _kst(23, 45), "US") is True


def test_unknown_shape_is_closed():
    assert is_market_open({}, _kst(10, 0), "KR") is False
    assert is_market_open(None, _kst(10, 0), "KR") is False


def test_malformed_time_is_closed():
    bad = {"today": {"integrated": {"regularMarket": {"startTime": "garbage", "endTime": "15:30"}}}}
    assert is_market_open(bad, _kst(10, 0), "KR") is False
