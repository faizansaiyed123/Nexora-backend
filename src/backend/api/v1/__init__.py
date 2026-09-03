from fastapi import APIRouter

from backend.api.v1.auth import router as auth_router
from backend.api.v1.health import router as health_router
from backend.api.v1.competitors import router as competitors_router
from backend.api.v1.sources import router as sources_router
from backend.api.v1.offerings import router as offerings_router
from backend.api.v1.offering_matches import router as offering_matches_router
from backend.api.v1.collections import router as collections_router


api_router = APIRouter(prefix="/v1")


api_router.include_router(
    health_router,
)

api_router.include_router(
    auth_router,
)

api_router.include_router(
    competitors_router,
    prefix="/competitors",
    tags=["Competitors"],
)

api_router.include_router(
    sources_router,
    prefix="/sources",
    tags=["Sources"],
)

api_router.include_router(
    offerings_router,
    prefix="/offerings",
    tags=["Offerings"],
)

api_router.include_router(
    offering_matches_router,
    prefix="/offering-matches",
    tags=["Offering Matches"],
)

api_router.include_router(
    collections_router,
    prefix="/collections",
    tags=["Collections"],
)
