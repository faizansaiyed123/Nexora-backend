from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.rate_limit import get_redis_client
from backend.db.session import get_db

router = APIRouter(
    prefix="/health",
    tags=["Health"],
)


@router.get("")
async def health_check(session: AsyncSession = Depends(get_db)) -> dict:
    checks = {"database": "ok", "redis": "ok"}

    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "down"

    try:
        redis = await get_redis_client()
        if redis is None:
            checks["redis"] = "down"
        else:
            await redis.ping()
    except Exception:
        checks["redis"] = "down"

    overall = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
    if overall != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"status": overall, "checks": checks})
    return {"status": overall, "checks": checks}
