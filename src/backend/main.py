import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.exception_handlers import app_exception_handler
from backend.api.v1.auth import router as auth_router
from backend.api.v1.health import router as health_router
from backend.api.v1.competitors import router as competitors_router
from backend.api.v1.sources import router as sources_router
from backend.api.v1.offerings import router as offerings_router
from backend.api.v1.offering_matches import router as offering_matches_router
from backend.api.v1.collections import router as collections_router
from backend.api.v1.discovery import router as discovery_router
from backend.api.v1.jobs import router as jobs_router
from backend.api.v1.alerts import router as alerts_router
from backend.core.config import get_settings
from backend.core.exceptions import AppException
from backend.core.one_time_maintenance import purge_public_application_data_once
from backend.services.url_security import SecurityValidationError

logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(
    title="Nexora Backend",
    version=settings.app_version,
    description="Backend API for Nexora competitive price intelligence.",
)

# app.add_middleware(
#     TrustedHostMiddleware,
#     allowed_hosts=settings.allowed_hosts,
# )
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




@app.on_event("startup")
async def _run_guarded_one_time_database_maintenance() -> None:
    # Dormant unless the explicit one-time flag and database-name confirmation
    # are set in the Render environment. This hook is removed after this reset.
    await purge_public_application_data_once()

# Include each router directly with /v1 prefix
app.include_router(health_router, prefix="/v1")
app.include_router(auth_router, prefix="/v1")
app.include_router(competitors_router, prefix="/v1")
app.include_router(sources_router, prefix="/v1")
app.include_router(offerings_router, prefix="/v1")
app.include_router(offering_matches_router, prefix="/v1")
app.include_router(collections_router, prefix="/v1")
app.include_router(discovery_router, prefix="/v1")
app.include_router(jobs_router, prefix="/v1")
app.include_router(alerts_router, prefix="/v1")


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Nexora Backend is running"}

@app.get("/debug/cors")
async def debug_cors():
    return {"cors_origins": settings.cors_origins, "allowed_hosts": settings.allowed_hosts}