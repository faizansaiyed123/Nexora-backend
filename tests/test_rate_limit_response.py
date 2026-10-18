import pytest
from starlette.requests import Request

from backend.api.exception_handlers import app_exception_handler
from backend.core.exceptions import RateLimitException


@pytest.mark.asyncio
async def test_rate_limit_response_exposes_retry_after_header_and_body():
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/auth/login",
        "raw_path": b"/v1/auth/login",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 12345),
    }
    response = await app_exception_handler(
        Request(scope),
        RateLimitException(
            message="Rate limit exceeded. Please try again in 37 seconds.",
            retry_after_seconds=37,
        ),
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "37"
    assert response.body
    import json
    payload = json.loads(response.body)
    assert payload["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert payload["error"]["retry_after_seconds"] == 37
