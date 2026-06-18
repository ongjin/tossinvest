import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pytossinvest_mcp.audit import AuditLog

fakeredis = pytest.importorskip("fakeredis")


def _fixed_clock():
    return datetime(2026, 6, 17, 1, 2, 3, tzinfo=timezone.utc)


def test_record_appends_jsonl_with_timestamp(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, now=_fixed_clock)
    log.record({"tool": "place_order", "decision": "placed"})
    log.record({"tool": "preview_order", "decision": "previewed"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["tool"] == "place_order"
    assert first["decision"] == "placed"
    assert first["ts"] == "2026-06-17T01:02:03+00:00"


def test_record_serializes_decimal(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, now=_fixed_clock)
    log.record({"tool": "place_order", "notional": Decimal("70000")})
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["notional"] == "70000"


def test_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "audit.log"
    AuditLog(path, now=_fixed_clock).record({"tool": "x"})
    assert path.exists()


def test_read_events_parses_and_skips_blank(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, now=_fixed_clock)
    log.record({"tool": "place_order", "decision": "placed",
                "notional": Decimal("70000"), "currency": "KRW"})
    log.record({"tool": "preview_order", "decision": "previewed"})
    events = log.read_events()
    assert [e["decision"] for e in events] == ["placed", "previewed"]
    assert events[0]["notional"] == "70000"  # serialized as string


def test_read_events_missing_file_is_empty(tmp_path):
    assert AuditLog(tmp_path / "nope.log").read_events() == []


def test_redis_audit_record_and_read():
    from pytossinvest_mcp.audit import RedisAuditSink

    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sink = RedisAuditSink(
        r,
        now=lambda: datetime(2026, 6, 18, tzinfo=timezone.utc),
    )
    sink.record({"tool": "place_order", "decision": "placed", "notional": "100"})
    events = sink.read_events()
    assert len(events) == 1
    assert events[0]["decision"] == "placed"
    assert events[0]["notional"] == "100"
    assert events[0]["ts"].startswith("2026-06-18")
