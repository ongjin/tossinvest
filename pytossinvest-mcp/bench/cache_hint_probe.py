"""Does SEP-2354 caching actually cover this server's expensive calls?

The June write-up assumed per-tool hints: a generous ttl on get_stock_info,
cacheScope=private on get_holdings. But hints key off CACHEABLE_METHODS, and
tools/call is not in it. This probe establishes what is actually cacheable,
what the hint looks like on the wire, and what a hint-honouring client saves.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "workspace/personal/toss/pytossinvest-mcp/tests"))

from starlette.testclient import TestClient

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.server import build_server, transport_kwargs
from pytossinvest_mcp.http import build_http_app
from conftest import FakeClient

from mcp.server.caching import CACHEABLE_METHODS, CacheHint
from mcp.shared.inbound import (
    PROTOCOL_VERSION_META_KEY, CLIENT_INFO_META_KEY, CLIENT_CAPABILITIES_META_KEY,
    MCP_PROTOCOL_VERSION_HEADER, MCP_METHOD_HEADER, MCP_NAME_HEADER,
    MODERN_PROTOCOL_VERSIONS, NAME_BEARING_METHODS,
)

MODERN = MODERN_PROTOCOL_VERSIONS[0]
AUTH = {"authorization": "Bearer probe-token", "content-type": "application/json",
        "accept": "application/json, text/event-stream"}

# What the June post wanted, expressed against the real API.
HINTS = {
    "tools/list": CacheHint(ttl_ms=3_600_000, scope="public"),   # 14 tools, changes on deploy
}


def build(hints=None):
    settings = Settings(_env_file=None, transport="http", auth_token="probe-token",
                        mode="paper", audit_log_path="/dev/null")
    mcp = build_server(settings, client=FakeClient(), cache_hints=hints)
    return build_http_app(mcp, auth_token="probe-token", **transport_kwargs(settings))


def envelope(method, params=None):
    p = dict(params or {})
    p["_meta"] = {
        PROTOCOL_VERSION_META_KEY: MODERN,
        CLIENT_INFO_META_KEY: {"name": "cache-probe", "version": "0"},
        CLIENT_CAPABILITIES_META_KEY: {},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": p}


def headers_for(method, params=None):
    h = dict(AUTH)
    h[MCP_PROTOCOL_VERSION_HEADER] = MODERN
    h[MCP_METHOD_HEADER] = method
    key = NAME_BEARING_METHODS.get(method)
    if key and params and key in params:
        h[MCP_NAME_HEADER] = params[key]
    return h


def call(client, method, params=None):
    raw = json.dumps(envelope(method, params), separators=(",", ":"))
    r = client.post("/mcp", content=raw, headers=headers_for(method, params))
    return r, _payload(r.text)


def _payload(text):
    """Unwrap an SSE `data:` line or a bare JSON body."""
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    try:
        return json.loads(text)
    except Exception:
        return None


out: dict = {"cacheable_methods": sorted(CACHEABLE_METHODS)}

# 1. Is tools/call cacheable? Ask the SDK directly.
try:
    build({"tools/call": CacheHint(ttl_ms=60_000, scope="public")})
    out["tools_call_hint_accepted"] = True
except Exception as e:
    out["tools_call_hint_accepted"] = False
    out["tools_call_hint_error"] = f"{type(e).__name__}: {e}"

# 2. Baseline: no hints. Does the wire carry cache metadata at all?
with TestClient(build(None)) as c:
    r, body = call(c, "tools/list", {})
    res = (body or {}).get("result", {})
    out["no_hint"] = {
        "status": r.status_code,
        "response_bytes": len(r.text),
        "ttlMs": res.get("ttlMs", res.get("ttl_ms")),
        "cacheScope": res.get("cacheScope", res.get("cache_scope")),
        "result_keys": sorted(res.keys()),
    }

# 3. With a hint on tools/list.
with TestClient(build(HINTS)) as c:
    r, body = call(c, "tools/list", {})
    res = (body or {}).get("result", {})
    out["with_hint"] = {
        "status": r.status_code,
        "response_bytes": len(r.text),
        "ttlMs": res.get("ttlMs", res.get("ttl_ms")),
        "cacheScope": res.get("cacheScope", res.get("cache_scope")),
        "result_keys": sorted(res.keys()),
    }

    # what a hint-honouring client skips: the whole discovery payload per session
    r2, _ = call(c, "tools/call", {"name": "get_accounts", "arguments": {}})
    out["with_hint"]["tools_call_response_bytes"] = len(r2.text)

# 4. server/discover — the v2 replacement for initialize-time capability exchange.
with TestClient(build(HINTS)) as c:
    r, body = call(c, "server/discover", {})
    out["server_discover"] = {
        "status": r.status_code,
        "response_bytes": len(r.text),
        "error": (body or {}).get("error"),
        "result_keys": sorted(((body or {}).get("result") or {}).keys()),
    }

print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
