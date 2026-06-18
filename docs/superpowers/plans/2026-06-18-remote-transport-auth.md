# Remote Transport + Endpoint Auth Implementation Plan (Phase 1, Plan 3/3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `pytossinvest-mcp` a remote **Streamable-HTTP** transport (selectable alongside the existing stdio) protected by a static **bearer** endpoint auth, plus a Docker deployment template — so a single-tenant user can self-host the server behind a load balancer with the already-externalized Redis state (Plans 1 & 2).

**Architecture:** A new mode axis `TOSSINVEST_TRANSPORT=stdio|http` (orthogonal to `mode` and `state_backend`). stdio stays the default and is byte-for-byte unchanged (`mcp.run()`). For http, `server.main` builds the FastMCP Streamable-HTTP ASGI app (`mcp.streamable_http_app()`, a Starlette app mounted at `/mcp`), wraps it in a constant-time bearer-check middleware, and serves it with uvicorn. Config validation gates http on a required `auth_token` (mirroring the existing `live⇒allow_live` and `redis⇒redis_url` double-gates). The 14 tools, the safety model, and both state backends are untouched — this plan only adds the transport/auth shell around them.

**Tech Stack:** Python 3.12, `mcp` (FastMCP, already a dep — provides `streamable_http_app()` on Starlette), `starlette` (transitive via `mcp`; middleware + `TestClient`), `uvicorn` (new optional `[http]` extra; only imported at runtime to serve), `pytest`. Builds on Plans 1 & 2 (Redis state backends already merged to `main`).

## Global Constraints

- **`pytossinvest-mcp` only** — the `pytossinvest` SDK is untouched; verify nothing under `pytossinvest/` changes.
- **stdio path is unchanged and is the default** — `TOSSINVEST_TRANSPORT` defaults to `stdio`; the stdio boot (`mcp.run()`) and all existing behavior must be byte-for-byte preserved. This is a backward-compat invariant: existing stdio users (Claude Desktop, etc.) see no change and need no new env var.
- **http endpoint auth is mandatory** — an exposed HTTP endpoint lets anyone who knows the URL place orders. The config validator MUST refuse to boot in http mode without `TOSSINVEST_AUTH_TOKEN` (fail-closed), exactly like `mode='live'` requires `TOSSINVEST_ALLOW_LIVE=1` and `state_backend='redis'` requires `TOSSINVEST_REDIS_URL`.
- **Bearer comparison is constant-time** — compare the presented token with `hmac.compare_digest`, never `==` (avoid timing oracles on the secret).
- **Single-tenant; no secrets in transport/state** — one credential (the user's own Toss keys via config). The bearer token authenticates the *endpoint*; it is not a per-user credential and is never stored in Redis.
- **No MCP OAuth / no multi-tenant** — a static configured bearer is the entire auth surface (spec §1.4 non-goal). Do not add an OAuth provider, token issuance, or user accounts.
- **Money/quantity are NEVER float** — strings/`Decimal` end-to-end. (This plan adds no money paths, but the rule still binds any code you touch.)
- **Tests: zero network, no live keys** — http is tested in-process with `starlette.testclient.TestClient` (no real socket); the uvicorn serve call is behind a thin wrapper that tests monkeypatch (uvicorn is never bound in a test).
- **Test imports** — `from conftest import ...` (never `from tests.conftest`).
- **No AI-authorship markers** anywhere (commit messages, comments, docs). Public OSS repo — be strict.
- **Commits happen on the feature branch per-task**; no push/merge without the user's request.
- **Commands run from repo root** `/Users/cyj/workspace/personal/toss`. MCP tests: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests`. Baseline before this plan: **MCP 151 / SDK 59 green**.
- **Branch**: do this on a fresh `feat/remote-transport-auth` branch off `main` (main already has Plans 1 & 2 merged).

## File Structure

- `pytossinvest-mcp/src/pytossinvest_mcp/config.py` — add `transport`, `http_host`, `http_port`, `auth_token` settings + an `http⇒auth_token` model validator (mirrors `_redis_requires_url`).
- `pytossinvest-mcp/src/pytossinvest_mcp/http.py` — **NEW**: `BearerAuthMiddleware` (constant-time check), `build_http_app(mcp, *, auth_token)` (wraps `mcp.streamable_http_app()`), `serve_http(app, *, host, port)` (thin uvicorn wrapper — the only place uvicorn is imported).
- `pytossinvest-mcp/src/pytossinvest_mcp/server.py` — `build_server` sets `stateless_http` when http; new `run_server(settings, mcp)` branches stdio vs http; `main` calls `run_server`.
- `pytossinvest-mcp/pyproject.toml` — add optional `[http]` extra (`uvicorn`) + a `filterwarnings` entry to silence the external `starlette.testclient`+`httpx` deprecation.
- `pytossinvest-mcp/tests/test_config.py` — extend: transport/auth defaults + validation.
- `pytossinvest-mcp/tests/test_http.py` — **NEW**: bearer middleware (401/pass) + `build_http_app` mounts auth on the real `/mcp` app.
- `pytossinvest-mcp/tests/test_transport.py` — **NEW**: `run_server` routes http→`serve_http` (auth-wrapped app, host/port) and stdio→`mcp.run()`.
- `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/.env.example` — **NEW**: app + Redis (AOF on) deployment template.
- `README.md` (package `pytossinvest-mcp/README.md`) — http-mode run instructions (folded into the deploy task).
- `CLAUDE.md`, `docs/claude/pytossinvest-mcp.md` — docs self-update (final task).

---

### Task 1: `config.py` — transport / http / auth settings + validation

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/config.py`
- Test: `pytossinvest-mcp/tests/test_config.py` (extend)

**Interfaces:**
- Produces:
  - `Settings.transport: Literal["stdio", "http"]` (default `"stdio"`)
  - `Settings.http_host: str` (default `"127.0.0.1"`), `Settings.http_port: int` (default `8000`)
  - `Settings.auth_token: str` (default `""`)
  - model validator `_http_requires_auth_token`: raises `ValueError` when `transport == "http"` and `auth_token` is empty.

- [ ] **Step 1: Write the failing tests**

Add to `pytossinvest-mcp/tests/test_config.py`:
```python
def test_transport_defaults_to_stdio():
    s = Settings(_env_file=None)
    assert s.transport == "stdio"
    assert s.http_host == "127.0.0.1"
    assert s.http_port == 8000
    assert s.auth_token == ""


def test_http_without_auth_token_raises():
    import pytest
    with pytest.raises(ValueError, match="TOSSINVEST_AUTH_TOKEN"):
        Settings(_env_file=None, transport="http")


def test_http_with_auth_token_ok():
    s = Settings(_env_file=None, transport="http", auth_token="secret",
                 http_host="0.0.0.0", http_port=9000)
    assert s.transport == "http"
    assert s.http_host == "0.0.0.0"
    assert s.http_port == 9000


def test_stdio_needs_no_auth_token():
    s = Settings(_env_file=None, transport="stdio")  # must not raise
    assert s.transport == "stdio"
```
(If `test_config.py` does not already `import pytest` / `from pytossinvest_mcp.config import Settings` at module top, add them — check the file first and match its existing imports.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_config.py -v`
Expected: FAIL — `Settings` has no `transport`/`auth_token` fields (the http-validation tests error or the default test fails on the missing attribute).

- [ ] **Step 3: Add the settings + validator**

In `config.py`, after the `# audit` block and the existing `# state backend (HA)` block (i.e. alongside the other config fields), add:
```python
    # remote transport (default stdio = unchanged). http requires auth_token too.
    transport: Literal["stdio", "http"] = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    auth_token: str = ""
```
And add a model validator next to `_redis_requires_url`:
```python
    @model_validator(mode="after")
    def _http_requires_auth_token(self):
        if self.transport == "http" and not self.auth_token:
            raise ValueError(
                "transport='http' requires TOSSINVEST_AUTH_TOKEN (an exposed "
                "endpoint must be authenticated)"
            )
        return self
```
(`Literal` and `model_validator` are already imported at the top of `config.py`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_config.py -v`
Expected: PASS (new tests + existing config tests still green).

- [ ] **Step 5: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/config.py pytossinvest-mcp/tests/test_config.py
git commit -m "feat(mcp): transport/http/auth_token settings + http requires auth_token"
```

---

### Task 2: `http.py` — bearer auth middleware + ASGI app assembly

**Files:**
- Create: `pytossinvest-mcp/src/pytossinvest_mcp/http.py`
- Modify: `pytossinvest-mcp/pyproject.toml`
- Test: `pytossinvest-mcp/tests/test_http.py` (new)

**Interfaces:**
- Consumes: a built `FastMCP` instance (its `.streamable_http_app()` returns a `starlette.applications.Starlette` mounted at `/mcp`); `Settings.auth_token`.
- Produces:
  - `class BearerAuthMiddleware(BaseHTTPMiddleware)` — 401s any request whose `Authorization` header is not `Bearer <auth_token>` (constant-time compare).
  - `def build_http_app(mcp, *, auth_token: str) -> Starlette` — returns `mcp.streamable_http_app()` with the bearer middleware added.
  - `def serve_http(app, *, host: str, port: int) -> None` — thin uvicorn runner (the ONLY place uvicorn is imported).

- [ ] **Step 1: Write the failing tests**

```python
# pytossinvest-mcp/tests/test_http.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_http.py -v`
Expected: FAIL — `cannot import name 'BearerAuthMiddleware'` / no module `pytossinvest_mcp.http`.

- [ ] **Step 3: Implement `http.py`**

```python
# pytossinvest-mcp/src/pytossinvest_mcp/http.py
from __future__ import annotations

import hmac

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request without a valid `Authorization: Bearer <token>` header.

    Constant-time comparison (hmac.compare_digest) avoids leaking the token via
    response timing. This is the entire endpoint-auth surface for http mode.
    """

    def __init__(self, app, *, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented, self._token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_http_app(mcp, *, auth_token: str) -> Starlette:
    """Wrap the FastMCP Streamable-HTTP ASGI app (mounted at /mcp) with bearer auth."""
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=auth_token)
    return app


def serve_http(app, *, host: str, port: int) -> None:  # pragma: no cover - thin uvicorn wrapper
    """Run the ASGI app with uvicorn. uvicorn is an optional ([http]) dependency,
    imported here so stdio installs and the test suite never need it."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
```

- [ ] **Step 4: Add the `[http]` extra + warning filter to `pyproject.toml`**

In `pytossinvest-mcp/pyproject.toml`, under `[project.optional-dependencies]` add the `http` extra:
```toml
[project.optional-dependencies]
dev = ["pytest>=8", "fakeredis[lua]>=2"]
redis = ["redis>=5"]
http = ["uvicorn>=0.30"]
```
And under `[tool.pytest.ini_options]` add a `filterwarnings` entry (the `starlette.testclient`+`httpx` deprecation is external noise — keep test output pristine):
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
markers = ["integration: requires a real Redis (opt-in, skipped by default)"]
filterwarnings = [
    "ignore:Using `httpx` with `starlette.testclient` is deprecated",
]
```

- [ ] **Step 5: Refresh the lockfile for the new extra**

The deploy Dockerfile (Task 4) installs with `--frozen`, so the root `uv.lock` must include the `[http]` extra's `uvicorn` (same as Plan 1 locked the `[redis]` extra).
Run: `uv lock`
Expected: `uv.lock` updated (or unchanged if `uvicorn` was already locked transitively). The repo stays installable with `--frozen`.

- [ ] **Step 6: Run to verify pass + pristine output**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_http.py -v`
Expected: PASS (4 tests). Confirm the warnings summary is **empty** (the filter silenced the testclient deprecation). If a deprecation still shows, widen the filter message to match the exact text printed.

- [ ] **Step 7: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/http.py pytossinvest-mcp/pyproject.toml uv.lock pytossinvest-mcp/tests/test_http.py
git commit -m "feat(mcp): http transport — bearer auth middleware + streamable ASGI app"
```

---

### Task 3: `server.py` — transport branch (`run_server`) + stateless_http

**Files:**
- Modify: `pytossinvest-mcp/src/pytossinvest_mcp/server.py`
- Test: `pytossinvest-mcp/tests/test_transport.py` (new)

**Interfaces:**
- Consumes: `Settings.transport/http_host/http_port/auth_token`; `build_http_app`/`serve_http` from `http.py`; a built `FastMCP`.
- Produces:
  - `build_server` constructs `FastMCP("pytossinvest-mcp", stateless_http=(settings.transport == "http"))`.
  - `def run_server(settings: Settings, mcp) -> None` — http → `serve_http(build_http_app(mcp, auth_token=...), host=..., port=...)`; stdio → `mcp.run()`.
  - `main` builds the client + server, then calls `run_server(settings, mcp)`.

- [ ] **Step 1: Write the failing tests**

```python
# pytossinvest-mcp/tests/test_transport.py
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_transport.py -v`
Expected: FAIL — `cannot import name 'run_server' from pytossinvest_mcp.server`.

- [ ] **Step 3: Add `run_server`, thread `stateless_http`, route `main`**

In `server.py`, change `build_server` to set `stateless_http`:
```python
def build_server(settings: Settings, *, client) -> FastMCP:
    app = build_app_context(settings, client=client)
    mcp = FastMCP("pytossinvest-mcp", stateless_http=(settings.transport == "http"))
    _register_reads(mcp, app)
    if settings.mode != "read_only":
        _register_writes(mcp, app)
    return mcp
```
Add `run_server` (place it just above `main`):
```python
def run_server(settings: Settings, mcp) -> None:
    if settings.transport == "http":
        from .http import build_http_app, serve_http
        app = build_http_app(mcp, auth_token=settings.auth_token)
        serve_http(app, host=settings.http_host, port=settings.http_port)
    else:
        mcp.run()  # stdio transport (default) for MCP clients like Claude Desktop
```
Change `main` to delegate to it (replace the trailing `mcp.run()`):
```python
def main() -> None:
    settings = Settings()
    from pytossinvest import TossInvestClient

    client = TossInvestClient(
        settings.client_id, settings.client_secret, base_url=settings.base_url
    )
    mcp = build_server(settings, client=client)
    run_server(settings, mcp)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests/test_transport.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite (no regression — stdio path unchanged)**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q`
Expected: PASS, 0 skips. Report the exact count (should be 151 baseline + new config/http/transport tests).

- [ ] **Step 6: Commit**

```bash
git add pytossinvest-mcp/src/pytossinvest_mcp/server.py pytossinvest-mcp/tests/test_transport.py
git commit -m "feat(mcp): route transport (stdio mcp.run vs http serve) + stateless_http"
```

---

### Task 4: Deployment template (Docker + Redis) + README http instructions

**Files:**
- Create: `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/.env.example`
- Modify: `pytossinvest-mcp/README.md`

**Interfaces:**
- Consumes: the `[redis]` + `[http]` extras; env vars `TOSSINVEST_TRANSPORT/HTTP_HOST/HTTP_PORT/AUTH_TOKEN/STATE_BACKEND/REDIS_URL/MODE/CLIENT_ID/CLIENT_SECRET`.
- Produces: a runnable `docker compose` stack (one app instance + Redis with AOF on). No code; deployment artifacts only.

- [ ] **Step 1: Create `deploy/Dockerfile`**

```dockerfile
# deploy/Dockerfile — build context is the repo root (uv workspace monorepo)
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir uv

# copy the whole workspace (root pyproject + both member packages)
COPY . .
RUN uv sync --package pytossinvest-mcp --extra redis --extra http --frozen

# http mode, bind all interfaces inside the container, redis state backend
ENV TOSSINVEST_TRANSPORT=http \
    TOSSINVEST_HTTP_HOST=0.0.0.0 \
    TOSSINVEST_HTTP_PORT=8000 \
    TOSSINVEST_STATE_BACKEND=redis

EXPOSE 8000
CMD ["uv", "run", "--package", "pytossinvest-mcp", "pytossinvest-mcp"]
```

- [ ] **Step 2: Create `deploy/docker-compose.yml`**

```yaml
# deploy/docker-compose.yml — single app instance + Redis (AOF on for durability).
# Scale instances behind your own load balancer; state is shared via Redis.
services:
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]   # AOF: spend/paper counters survive restart
    volumes:
      - redis-data:/data
    restart: unless-stopped

  mcp:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    environment:
      TOSSINVEST_TRANSPORT: http
      TOSSINVEST_HTTP_HOST: 0.0.0.0
      TOSSINVEST_HTTP_PORT: "8000"
      TOSSINVEST_AUTH_TOKEN: ${TOSSINVEST_AUTH_TOKEN:?set TOSSINVEST_AUTH_TOKEN in .env}
      TOSSINVEST_STATE_BACKEND: redis
      TOSSINVEST_REDIS_URL: redis://redis:6379/0
      TOSSINVEST_MODE: ${TOSSINVEST_MODE:-paper}
      TOSSINVEST_CLIENT_ID: ${TOSSINVEST_CLIENT_ID:-}
      TOSSINVEST_CLIENT_SECRET: ${TOSSINVEST_CLIENT_SECRET:-}
    depends_on:
      - redis
    ports:
      - "8000:8000"
    restart: unless-stopped

volumes:
  redis-data:
```

- [ ] **Step 3: Create `deploy/.env.example`**

```bash
# deploy/.env.example — copy to deploy/.env and fill in. NEVER commit the real .env.
# A long random bearer; the MCP endpoint refuses to boot in http mode without it.
TOSSINVEST_AUTH_TOKEN=change-me-to-a-long-random-secret
# paper (default, no live keys needed) | live (also needs TOSSINVEST_ALLOW_LIVE=1)
TOSSINVEST_MODE=paper
# your own Toss Open API credentials (from WTS 설정 > Open API). Stay on your infra.
TOSSINVEST_CLIENT_ID=
TOSSINVEST_CLIENT_SECRET=
```

- [ ] **Step 4: Add an http-mode section to `pytossinvest-mcp/README.md`**

Add a short section (match the README's existing heading style/language). It must state: http mode requires `TOSSINVEST_AUTH_TOKEN` (the server refuses to boot without it); the MCP endpoint is served at `/mcp`; clients send `Authorization: Bearer <token>`; and the quickstart:
```bash
# remote http mode (single command, app + Redis)
cd deploy
cp .env.example .env   # then edit .env (set TOSSINVEST_AUTH_TOKEN, keys)
docker compose up --build
# → MCP Streamable-HTTP at http://localhost:8000/mcp  (Authorization: Bearer <token>)
```
Also note stdio remains the default (`TOSSINVEST_TRANSPORT` unset) for local Claude Desktop use — no change for existing users.

- [ ] **Step 5: Validate the template**

If `docker` is available:
Run: `docker compose -f deploy/docker-compose.yml config -q`
Expected: exits 0 with no error (compose file + env interpolation valid). The `:?` guard on `TOSSINVEST_AUTH_TOKEN` will error if no `.env`/env var is set — set a dummy (`TOSSINVEST_AUTH_TOKEN=x docker compose -f deploy/docker-compose.yml config -q`) to validate.
If `docker` is NOT available in this environment: state that in the report and instead confirm the YAML is well-formed by reading all three files back and checking the service/volume keys against Steps 1–3. (Deployment artifacts have no unit test; the gate is a careful read + the compose-config check where possible.)

- [ ] **Step 6: Confirm the test suite is unaffected**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q`
Expected: PASS, same count as Task 3 (deploy files add no tests and change no code).

- [ ] **Step 7: Commit**

```bash
git add deploy/Dockerfile deploy/docker-compose.yml deploy/.env.example pytossinvest-mcp/README.md
git commit -m "feat(mcp): docker deploy template (app + redis AOF) + http run docs"
```

---

### Task 5: Docs self-update (CLAUDE.md + docs/claude/pytossinvest-mcp.md)

**Files:**
- Modify: `CLAUDE.md`, `docs/claude/pytossinvest-mcp.md`

- [ ] **Step 1: Update `CLAUDE.md`**
- Commands: update the MCP test count to the new total (run the suite first to get the exact number).
- Conventions (설정 line): append the new env vars to the `TOSSINVEST_` list — `TRANSPORT`/`HTTP_HOST`/`HTTP_PORT`/`AUTH_TOKEN`.
- MCP 안전모델 / Conventions: add the new **transport axis** — `TOSSINVEST_TRANSPORT`=`stdio`(기본·무변경) / `http`(원격, bearer 인증 필수). Note it is orthogonal to `mode` (read_only/paper/live) and `state_backend` (memory/redis).
- 함정 (one line): http 모드는 `TOSSINVEST_AUTH_TOKEN` 없이 부팅 거부(config validator 삼중 게이트 — live/redis 와 동형 fail-closed); bearer 는 `hmac.compare_digest` 상수시간 비교; MCP 엔드포인트는 `/mcp` (Streamable HTTP, `stateless_http=True`); uvicorn 은 옵션 `[http]` extra, 런타임에만 import.
- Remove/replace any remaining "transport 는 현재 stdio 단일" wording (stdio 는 이제 기본이고 http 가 선택지).

- [ ] **Step 2: Update `docs/claude/pytossinvest-mcp.md`**
- Add `http.py` to the module map: ASGI 조립 + bearer 미들웨어 (`BearerAuthMiddleware`/`build_http_app`/`serve_http`).
- Add a transport section: `TOSSINVEST_TRANSPORT=stdio|http`; stdio=`mcp.run()` (기본·무변경); http=`mcp.streamable_http_app()`(Starlette, `/mcp`) + `BearerAuthMiddleware` + uvicorn(`serve_http`). `build_server` sets `stateless_http` when http; `run_server` branches.
- Add the auth pitfall: http⇒auth_token 필수(부팅 거부), 상수시간 bearer 비교, 단일테넌트(토큰은 엔드포인트 인증일 뿐 유저 자격증명/Redis 미저장).
- Update the `한계`/transport wording to reflect stdio|http (remote http now exists; multi-instance HA = http + redis backend).

- [ ] **Step 3: Verify suites**

Run: `uv run --package pytossinvest-mcp pytest pytossinvest-mcp/tests -q` (green) and `uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q` (59, untouched).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/claude/pytossinvest-mcp.md
git commit -m "docs(mcp): remote http transport + bearer endpoint auth"
```

---

## Self-Review

**1. Spec coverage** (spec = `docs/superpowers/specs/2026-06-18-self-host-remote-mcp-design.md`):
- §1.3 / §2 decision 1 (remote transport, stdio|http selectable, stdio unchanged) → Task 1 (`transport` setting) + Task 3 (`run_server` branch, `stateless_http`). ✅
- §1.3 / §2 decision 2 / §5 (endpoint bearer auth, http-only, boot-refuse without token, 401 on bad bearer) → Task 1 (`_http_requires_auth_token` validator) + Task 2 (`BearerAuthMiddleware`, `build_http_app`). ✅
- §3 module table: `config.py` (+transport/http/auth) → Task 1; **new `http.py`** (ASGI + bearer) → Task 2; `server.py` (transport branch) → Task 3; `tools.py` unchanged (not touched — verified by full-suite regression in Tasks 3/4/5). ✅
- §3 dependency `uvicorn` (optional `[http]`) → Task 2 (pyproject extra). ✅
- §6 test strategy: regression guard (full suite green, Tasks 3/5) ✅; transport/auth tests — bearer 누락/오류 401·정상 통과 (Task 2), config 검증 http⇒auth_token (Task 1), stdio 무변경 스모크 (Task 3 `run_server` stdio + full-suite). ✅
- §7 env vars (TRANSPORT/HTTP_HOST/HTTP_PORT/AUTH_TOKEN) → Task 1 (settings) + Task 5 (docs). ✅
- §8 Phase 1 deployment template (Docker compose: app + Redis, AOF on) → Task 4. ✅
- §부록 invariant: new mode axis `transport`(stdio|http) orthogonal to `mode`/`state_backend` → Task 5 (docs). ✅
- **Out of scope (correctly not built):** state backends / redis stores (Plans 1 & 2, already merged); shared rate limiter, MCP OAuth, multi-tenant (spec §1.4 non-goals).

**2. Placeholder scan:** every code step contains complete code (config fields + validator, full `http.py`, `run_server`/`build_server`/`main` bodies, all four test files, Dockerfile/compose/.env). Deployment validation (Task 4 Step 5) is explicitly conditional on `docker` availability with a concrete fallback — not a "TBD". No "add error handling"/"similar to" placeholders.

**3. Type consistency:** `build_http_app(mcp, *, auth_token: str) -> Starlette` and `serve_http(app, *, host: str, port: int)` are defined in Task 2 and called identically in Task 3's `run_server`. `BearerAuthMiddleware(app, *, token=...)` constructed the same way in the Task 2 tests and in `build_http_app`. `Settings.transport/http_host/http_port/auth_token` defined in Task 1 and consumed unchanged in Task 3 and the deploy env (Task 4). `run_server(settings, mcp)` defined in Task 3 and is the sole transport entry from `main`.

**Backward-compat check:** stdio is the default (`transport="stdio"`), `build_server` sets `stateless_http=False` for stdio, and `run_server` stdio branch is exactly the old `mcp.run()` — existing stdio users and the full existing test suite are unaffected (verified by the 0-regression full-suite runs in Tasks 3, 4, 5).

**Security check:** an http endpoint cannot boot without `auth_token` (Task 1 validator, fail-closed like live/redis gates); the bearer is compared constant-time with `hmac.compare_digest` (Task 2); the token is endpoint auth only — never stored in Redis or used as a Toss credential (single-tenant, spec §1.4). The `.env.example` ships a placeholder secret and instructs not to commit the real `.env`.

**No-network-test check:** http is tested with in-process `starlette.testclient.TestClient` (no socket); `serve_http` (the only uvicorn import) is monkeypatched in Task 3's tests, so uvicorn is never bound during the suite and the `[http]` extra is not required to run tests.
