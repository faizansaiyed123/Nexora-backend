from fastapi import FastAPI
from backend.api.v1 import api_router
from backend.api.exception_handlers import app_exception_handler
from backend.core.exceptions import AppException

app = FastAPI(
    title="Nexora Backend",
    version="0.1.0",
    description="Backend API for Nexora.",
)

app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(AppException, app_exception_handler)

app.include_router(api_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Nexora Backend is running"}
