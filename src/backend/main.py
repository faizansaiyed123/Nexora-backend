import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api.exception_handlers import app_exception_handler
from backend.api.v1 import api_router
from backend.core.config import get_settings
from backend.core.exceptions import AppException
from backend.services.url_security import SecurityValidationError

logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(
    title="Nexora Backend",
    version=settings.app_version,
    description="Backend API for Nexora competitive price intelligence.",
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.allowed_hosts,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    expose_headers=[
        "Content-Disposition",
        "X-Total-Count",
        "X-Page-Count",
        "Retry-After",
    ],
)

app.add_exception_handler(AppException, app_exception_handler)


@app.exception_handler(SecurityValidationError)
async def security_validation_error_handler(
    request: Request,
    exc: SecurityValidationError,
) -> JSONResponse:
    """Security validation failures must reach the browser.

    SecurityValidationError is a ValueError and is not an AppException, so it
    previously escaped every registered handler. Because CORSMiddleware runs
    before exception handlers, an unhandled 500 never received
    Access-Control-Allow-Origin and the browser discarded the response entirely,
    surfacing only "Failed to fetch".
    """
    logger.warning("Security validation rejected request on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {
                "code": "SECURITY_VALIDATION_FAILED",
                "message": str(exc),
            },
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Catch-all that preserves CORS headers for any unhandled error.

    CORSMiddleware runs before routing, so a 500 raised inside a request never
    gets Access-Control-Allow-Origin on its response and the browser blocks it.
    Returning the response here (after CORS has already added its headers)
    guarantees the client receives a structured error instead of a silent
    network failure.
    """
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred. Please try again later.",
            },
        },
    )


app.include_router(api_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Nexora Backend is running"}
