from __future__ import annotations

__all__ = [
    "TossInvestError",
    "AuthError",
    "ForbiddenError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "BusinessRuleError",
    "RateLimitError",
    "ServerError",
    "OAuthError",
    "error_from_response",
    "oauth_error_from_response",
]


class TossInvestError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        http_status: int | None = None,
        request_id: str | None = None,
        data: dict | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(f"[{http_status}] {code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.request_id = request_id
        self.data = data or {}
        self.retry_after = retry_after


class AuthError(TossInvestError): ...
class ForbiddenError(TossInvestError): ...
class NotFoundError(TossInvestError): ...
class ValidationError(TossInvestError): ...
class ConflictError(TossInvestError): ...
class BusinessRuleError(TossInvestError): ...
class RateLimitError(TossInvestError): ...
class ServerError(TossInvestError): ...
class OAuthError(TossInvestError): ...


_STATUS_MAP: dict[int, type[TossInvestError]] = {
    400: ValidationError,
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    422: BusinessRuleError,
    429: RateLimitError,
}


def error_from_response(
    http_status: int, body: dict | None, headers: dict | None = None
) -> TossInvestError:
    headers = headers or {}
    err = (body or {}).get("error") or {}
    code = err.get("code", "unknown")
    message = err.get("message", "")
    request_id = err.get("requestId")
    data = err.get("data")

    retry_after = None
    if http_status == 429:
        raw = headers.get("Retry-After")
        if raw is not None:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = None

    cls = _STATUS_MAP.get(http_status)
    if cls is None:
        cls = ServerError if http_status >= 500 else TossInvestError

    return cls(
        code,
        message,
        http_status=http_status,
        request_id=request_id,
        data=data,
        retry_after=retry_after,
    )


def oauth_error_from_response(http_status: int, body: dict | None) -> OAuthError:
    body = body or {}
    return OAuthError(
        body.get("error", "unknown"),
        body.get("error_description", ""),
        http_status=http_status,
    )
