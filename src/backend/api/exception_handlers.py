from fastapi import Request
from fastapi.responses import JSONResponse

from backend.core.exceptions import AppException, RateLimitException


async def app_exception_handler(
    request: Request,
    exc: AppException,
) -> JSONResponse:
    content = {
        "success": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
        },
    }
    headers: dict[str, str] = {}

    if isinstance(exc, RateLimitException):
        retry_after = max(1, int(exc.retry_after_seconds))
        content["error"]["retry_after_seconds"] = retry_after
        headers["Retry-After"] = str(retry_after)

    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=headers,
    )
