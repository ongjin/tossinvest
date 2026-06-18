from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from pytossinvest_mcp.http import BearerAuthMiddleware, build_http_app


async def _ok(request):
    return PlainTextResponse("ok")


def _guarded_app(token):
    app = Starlette(routes=[Route("/x", _ok)])
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def test_bearer_rejects_missing_header():
    client = TestClient(_guarded_app("secret"))
    assert client.get("/x").status_code == 401


def test_bearer_rejects_wrong_token():
    client = TestClient(_guarded_app("secret"))
    assert client.get("/x", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_bearer_allows_correct_token():
    client = TestClient(_guarded_app("secret"))
    r = client.get("/x", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.text == "ok"


def test_build_http_app_mounts_auth_on_real_mcp_endpoint():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test", stateless_http=True)
    app = build_http_app(mcp, auth_token="secret")
    client = TestClient(app)
    # the streamable endpoint is /mcp; without a bearer the middleware 401s
    # BEFORE any MCP handling, proving the guard wraps the real app.
    assert client.get("/mcp").status_code == 401


def test_http_accepts_non_localhost_host(tmp_path):
    from conftest import FakeClient
    from pytossinvest_mcp.config import Settings
    from pytossinvest_mcp.server import build_server
    from pytossinvest_mcp.http import build_http_app

    settings = Settings(_env_file=None, transport="http", auth_token="secret",
                        mode="read_only", audit_log_path=str(tmp_path / "audit.log"))
    mcp = build_server(settings, client=FakeClient())
    app = build_http_app(mcp, auth_token="secret")

    # A real MCP initialize, authorized, with a NON-localhost Host (what a remote client / proxy sends).
    # Pre-fix: FastMCP's localhost-only DNS-rebinding default 421s this request.
    # Post-fix: bearer auth is the entire auth surface; deploy host is proxy-controlled.
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    with TestClient(app) as client:
        r = client.post("/mcp", json=init, headers={
            "Authorization": "Bearer secret",
            "Accept": "application/json, text/event-stream",
            "Host": "mcp.example.com",
        })
    assert r.status_code != 421          # the bug: localhost-only DNS-rebinding default
    assert r.status_code == 200          # initialize succeeds through the wrapped /mcp app


def _build_app_allowed_hosts(tmp_path, allowed):
    from conftest import FakeClient
    from pytossinvest_mcp.config import Settings
    from pytossinvest_mcp.server import build_server
    from pytossinvest_mcp.http import build_http_app

    settings = Settings(_env_file=None, transport="http", auth_token="secret",
                        mode="read_only", http_allowed_hosts=allowed,
                        audit_log_path=str(tmp_path / "audit.log"))
    mcp = build_server(settings, client=FakeClient())
    return build_http_app(mcp, auth_token="secret")


def _initialize(client, host):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    return client.post("/mcp", json=init, headers={
        "Authorization": "Bearer secret",
        "Accept": "application/json, text/event-stream",
        "Host": host,
    })


def test_http_allowed_hosts_accepts_listed_host(tmp_path):
    # opt-in host pinning: when http_allowed_hosts is set, DNS-rebinding protection
    # is re-enabled and only listed Host headers pass (defense-in-depth on top of bearer).
    app = _build_app_allowed_hosts(tmp_path, ["mcp.example.com"])
    with TestClient(app) as client:
        r = _initialize(client, "mcp.example.com")
    assert r.status_code == 200


def test_http_allowed_hosts_rejects_unlisted_host(tmp_path):
    app = _build_app_allowed_hosts(tmp_path, ["mcp.example.com"])
    with TestClient(app) as client:
        r = _initialize(client, "evil.example.com")
    assert r.status_code == 421          # not in the allowlist → DNS-rebinding guard 421s
