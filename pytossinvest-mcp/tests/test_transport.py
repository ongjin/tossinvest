from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.server import run_server


def test_run_server_http_serves_auth_wrapped_app(monkeypatch):
    from mcp.server.fastmcp import FastMCP
    import pytossinvest_mcp.http as http_mod

    captured = {}

    def fake_serve(app, *, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(http_mod, "serve_http", fake_serve)

    mcp = FastMCP("t", stateless_http=True)
    settings = Settings(_env_file=None, transport="http", auth_token="secret",
                        http_host="0.0.0.0", http_port=9999, mode="read_only")
    run_server(settings, mcp)

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
    assert captured["app"] is not None          # the auth-wrapped ASGI app, not the bare mcp


def test_run_server_stdio_calls_mcp_run(monkeypatch):
    import pytossinvest_mcp.http as http_mod

    served = []
    monkeypatch.setattr(http_mod, "serve_http",
                        lambda *a, **k: served.append("http"))

    calls = []

    class StubMcp:
        def run(self):
            calls.append("run")

    settings = Settings(_env_file=None, transport="stdio", mode="read_only")
    run_server(settings, StubMcp())

    assert calls == ["run"]
    assert served == []                          # http path untouched in stdio mode
