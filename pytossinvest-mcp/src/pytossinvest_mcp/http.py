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
