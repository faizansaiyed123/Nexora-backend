from fastapi import FastAPI

from backend.api.exception_handlers import app_exception_handler
from backend.core.exceptions import AppException

app = FastAPI(
    title="Nexora Backend",
)

app.add_exception_handler(AppException, app_exception_handler)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Nexora Backend is running"}
