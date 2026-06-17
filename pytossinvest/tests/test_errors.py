from pytossinvest.errors import (
    TossInvestError,
    AuthError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    ConflictError,
    BusinessRuleError,
    RateLimitError,
    ServerError,
    OAuthError,
    error_from_response,
    oauth_error_from_response,
)


def _body(code, message="", data=None, request_id="01HXY"):
    err = {"code": code, "message": message, "requestId": request_id}
    if data is not None:
        err["data"] = data
    return {"error": err}


def test_maps_status_to_class():
    assert isinstance(error_from_response(400, _body("invalid-request")), ValidationError)
    assert isinstance(error_from_response(401, _body("expired-token")), AuthError)
    assert isinstance(error_from_response(403, _body("forbidden")), ForbiddenError)
    assert isinstance(error_from_response(404, _body("order-not-found")), NotFoundError)
    assert isinstance(error_from_response(409, _body("already-filled")), ConflictError)
    assert isinstance(error_from_response(422, _body("insufficient-buying-power")), BusinessRuleError)
    assert isinstance(error_from_response(500, _body("internal-error")), ServerError)


def test_preserves_code_and_metadata():
    err = error_from_response(
        422, _body("price-out-of-range", "bad", data={"field": "price"})
    )
    assert err.code == "price-out-of-range"
    assert err.request_id == "01HXY"
    assert err.data == {"field": "price"}
    assert err.http_status == 422


def test_rate_limit_reads_retry_after():
    err = error_from_response(429, _body("rate-limit-exceeded"), headers={"Retry-After": "3"})
    assert isinstance(err, RateLimitError)
    assert err.retry_after == 3.0


def test_unknown_code_does_not_crash():
    err = error_from_response(400, _body("brand-new-code-from-server"))
    assert isinstance(err, ValidationError)
    assert err.code == "brand-new-code-from-server"


def test_unknown_status_falls_back_to_base():
    err = error_from_response(418, _body("teapot"))
    assert type(err) is TossInvestError
    assert err.code == "teapot"


def test_empty_message_is_tolerated():
    err = error_from_response(401, {"error": {"code": "invalid-token"}})
    assert err.code == "invalid-token"
    assert err.message == ""


def test_oauth_error_separate_format():
    err = oauth_error_from_response(401, {"error": "invalid_client", "error_description": "bad secret"})
    assert isinstance(err, OAuthError)
    assert err.code == "invalid_client"
    assert err.message == "bad secret"
