"""Capture the raw JSON-RPC/HTTP exchange a client must perform to call one tool.

Runs the server in-process (paper mode, no credentials) over Streamable HTTP and
records every request/response so v1 and v2 can be compared on the same axis:
how many round trips, how many bytes, before the first useful answer.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve()
sys.path.insert(0, str(Path.home() / "workspace/personal/toss/pytossinvest-mcp/tests"))

from starlette.testclient import TestClient

from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.server import build_server
from pytossinvest_mcp.http import build_http_app
from conftest import FakeClient

PROTOCOL = os.environ.get("PROBE_PROTOCOL", "2025-06-18")
BASE_HEADERS = {
    "authorization": "Bearer probe-token",
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

exchange: list[dict] = []


def rpc(client, label, payload, extra=None):
    headers = dict(BASE_HEADERS)
    if extra:
        headers.update(extra)
    raw = json.dumps(payload)
    t0 = time.perf_counter()
    resp = client.post("/mcp", content=raw, headers=headers)
    dt = (time.perf_counter() - t0) * 1000
    body = resp.text
    exchange.append({
        "label": label,
        "status": resp.status_code,
        "ms": round(dt, 2),
        "request_bytes": len(raw),
        "response_bytes": len(body),
        "session_header": resp.headers.get("mcp-session-id"),
        "content_type": resp.headers.get("content-type"),
        "request": payload,
        "response": body[:500],
    })
    return resp, body


def build():
    settings = Settings(
        _env_file=None, transport="http", auth_token="probe-token",
        mode="paper", audit_log_path="/dev/null",
    )
    mcp = build_server(settings, client=FakeClient())
    kwargs = {}
    try:  # v2 moved transport options onto streamable_http_app()
        from pytossinvest_mcp.server import transport_kwargs
        kwargs = transport_kwargs(settings)
    except ImportError:
        pass
    return build_http_app(mcp, auth_token="probe-token", **kwargs)


def main() -> None:
    app = build()

    # --- Probe A: the standard client flow (handshake, then work) ---
    with TestClient(app) as client:
        resp, _ = rpc(client, "A1 initialize", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "wire-probe", "version": "0"},
            },
        })
        sid = resp.headers.get("mcp-session-id")
        session_hdr = {"mcp-protocol-version": PROTOCOL}
        if sid:
            session_hdr["mcp-session-id"] = sid

        rpc(client, "A2 notifications/initialized", {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, session_hdr)

        rpc(client, "A3 tools/list", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, session_hdr)

        rpc(client, "A4 tools/call get_accounts", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_accounts", "arguments": {}},
        }, session_hdr)

    # --- Probe B: cold call. No initialize at all. Does stateless accept it? ---
    with TestClient(build()) as client:
        rpc(client, "B1 tools/call (no handshake)", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_accounts", "arguments": {}},
        })

    # --- Probe C: discovery without initialize ---
    with TestClient(build()) as client:
        rpc(client, "C1 tools/list (no handshake)", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })

    import mcp as _mcp
    try:
        from importlib.metadata import version as _v
        sdk = _v("mcp")
    except Exception:
        sdk = getattr(_mcp, "__version__", "unknown")

    print(json.dumps({
        "sdk_version": sdk,
        "protocol_sent": PROTOCOL,
        "exchange": exchange,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
