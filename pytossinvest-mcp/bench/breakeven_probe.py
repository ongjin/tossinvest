"""What does the 2026-07-28 sessionless envelope actually cost?

The trade: you delete the initialize handshake (2 round trips, once per connection)
but every single request must now self-describe via params._meta + matching headers.
So there is a break-even: after N tool calls the envelope you pay forever exceeds the
handshake you saved once.

Measures both flows against the same server and solves for N.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / "workspace/personal/toss/pytossinvest-mcp/tests"))

from starlette.testclient import TestClient

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.server import build_server, transport_kwargs
from pytossinvest_mcp.http import build_http_app
from conftest import FakeClient

from mcp.shared.inbound import (
    PROTOCOL_VERSION_META_KEY, CLIENT_INFO_META_KEY, CLIENT_CAPABILITIES_META_KEY,
    MCP_PROTOCOL_VERSION_HEADER, MCP_METHOD_HEADER, MCP_NAME_HEADER,
    MODERN_PROTOCOL_VERSIONS, NAME_BEARING_METHODS,
)

MODERN = MODERN_PROTOCOL_VERSIONS[0]
LEGACY = "2025-06-18"
CLIENT_INFO = {"name": "wire-probe", "version": "0"}
CLIENT_CAPS: dict = {}

AUTH = {"authorization": "Bearer probe-token", "content-type": "application/json",
        "accept": "application/json, text/event-stream"}


def build():
    settings = Settings(_env_file=None, transport="http", auth_token="probe-token",
                        mode="paper", audit_log_path="/dev/null")
    mcp = build_server(settings, client=FakeClient())
    return build_http_app(mcp, auth_token="probe-token", **transport_kwargs(settings))


def post(client, payload, extra=None):
    headers = dict(AUTH)
    if extra:
        headers.update(extra)
    raw = json.dumps(payload, separators=(",", ":"))
    t0 = time.perf_counter()
    r = client.post("/mcp", content=raw, headers=headers)
    ms = (time.perf_counter() - t0) * 1000
    # bytes actually put on the wire: request line body + the MCP-specific headers
    hdr_bytes = sum(len(k) + len(v) + 4 for k, v in (extra or {}).items())
    return {"status": r.status_code, "ms": ms, "body_bytes": len(raw),
            "header_bytes": hdr_bytes, "wire_bytes": len(raw) + hdr_bytes,
            "response_bytes": len(r.text), "error": _err(r.text)}


def _err(text):
    if text.startswith("{"):
        try:
            return json.loads(text).get("error")
        except Exception:
            return None
    return None


def envelope(method, params=None):
    """A 2026-07-28 self-describing request: reserved _meta keys on every call."""
    p = dict(params or {})
    p["_meta"] = {
        PROTOCOL_VERSION_META_KEY: MODERN,
        CLIENT_INFO_META_KEY: CLIENT_INFO,
        CLIENT_CAPABILITIES_META_KEY: CLIENT_CAPS,
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}


def modern_headers(method, params=None):
    h = {MCP_PROTOCOL_VERSION_HEADER: MODERN, MCP_METHOD_HEADER: method}
    key = NAME_BEARING_METHODS.get(method)
    if key and params and key in params:
        h[MCP_NAME_HEADER] = params[key]
    return h


CALL_PARAMS = {"name": "get_accounts", "arguments": {}}

results: dict = {}

# ---------- legacy flow: handshake once, then bare requests ----------
with TestClient(build()) as c:
    legacy = {}
    legacy["initialize"] = post(c, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": CLIENT_INFO}})
    legacy["initialized"] = post(c, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    legacy["tools_call"] = post(c, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": CALL_PARAMS})
    results["legacy"] = legacy

# ---------- modern flow: no handshake, envelope on every request ----------
with TestClient(build()) as c:
    modern = {}
    modern["tools_call"] = post(c, envelope("tools/call", CALL_PARAMS),
                                modern_headers("tools/call", CALL_PARAMS))
    modern["tools_list"] = post(c, envelope("tools/list", {}), modern_headers("tools/list", {}))
    # what happens if the header disagrees with the body (SEP-2243 header routing)
    bad = dict(modern_headers("tools/call", CALL_PARAMS))
    bad[MCP_NAME_HEADER] = "place_order"
    modern["header_body_mismatch"] = post(c, envelope("tools/call", CALL_PARAMS), bad)
    results["modern"] = modern

# ---------- break-even ----------
handshake_saved = (results["legacy"]["initialize"]["wire_bytes"]
                   + results["legacy"]["initialize"]["response_bytes"]
                   + results["legacy"]["initialized"]["wire_bytes"]
                   + results["legacy"]["initialized"]["response_bytes"])
per_call_extra = (results["modern"]["tools_call"]["wire_bytes"]
                  - results["legacy"]["tools_call"]["wire_bytes"])

results["summary"] = {
    "handshake_bytes_saved_once": handshake_saved,
    "envelope_bytes_per_request": per_call_extra,
    "break_even_calls": (handshake_saved / per_call_extra) if per_call_extra else None,
    "legacy_round_trips_before_first_answer": 3,
    "modern_round_trips_before_first_answer": 1,
}

print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
