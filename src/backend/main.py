from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api.exception_handlers import app_exception_handler
from backend.api.v1 import api_router
from backend.core.config import get_settings
from backend.core.exceptions import AppException

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
)

app.add_exception_handler(AppException, app_exception_handler)
app.include_router(api_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Nexora Backend is running"}
