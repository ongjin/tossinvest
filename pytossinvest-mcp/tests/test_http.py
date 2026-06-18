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
