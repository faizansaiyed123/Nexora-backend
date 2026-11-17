"""Safe, dependency-free recruiter demo endpoints.

These routes are intentionally isolated from the production persistence and collection
pipeline. They provide a small executable representation of Nexora's core intelligence
workflow without requiring PostgreSQL, Redis, Celery, SMTP, or outbound scraping.
"""

from datetime import datetime, timezone
from fastapi import APIRouter

router = APIRouter(prefix="/demo", tags=["Demo"])

_PRICE = {"current": 1249.0, "previous": 1299.0}


@router.get("/overview")
async def demo_overview() -> dict:
    change = ((_PRICE["current"] - _PRICE["previous"]) / _PRICE["previous"]) * 100
    return {
        "workspace": "Acme Commerce",
        "role": "ORG_ADMIN",
        "tracked_offerings": 3,
        "competitors": 2,
        "sources": 2,
        "active_jobs": 0,
        "alerts": 1,
        "featured_signal": {
            "offering": "Apex Pro 14 Laptop",
            "competitor": "Northstar Market",
            "current_price": _PRICE["current"],
            "previous_price": _PRICE["previous"],
            "percentage_change": round(change, 2),
            "status": "DECREASE",
        },
    }


@router.post("/collect")
async def demo_collect() -> dict:
    _PRICE["previous"] = _PRICE["current"]
    _PRICE["current"] = round(_PRICE["current"] * 0.96, 2)
    change = ((_PRICE["current"] - _PRICE["previous"]) / _PRICE["previous"]) * 100
    return {
        "job_id": "demo-job-live",
        "status": "COMPLETED",
        "message": "Demo collection completed successfully.",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "observation": {
            "price": _PRICE["current"],
            "currency": "USD",
            "availability": "IN_STOCK",
            "confidence": "HIGH",
        },
        "snapshot": {
            "current_price": _PRICE["current"],
            "previous_price": _PRICE["previous"],
            "percentage_difference": round(change, 2),
            "price_movement": "DECREASE",
        },
        "alert": {
            "triggered": True,
            "type": "PRICE_CHANGE",
            "message": "Competitor price movement detected.",
        },
    }
