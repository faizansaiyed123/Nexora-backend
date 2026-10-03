"""
Website Discovery API Endpoints.
Provides client website analysis, product catalog auto-discovery,
dynamic attribute schema extraction, and job progress tracking.
"""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import (
    AuthenticatedUserContext,
    require_admin,
    require_analyst,
    require_reader,
)
from backend.db.session import get_db
from backend.models.enums import RoleEnum
from backend.schemas.discovery import (
    DiscoveryJobResponse,
    DiscoveryRunRequest,
)
from backend.schemas.errors import ErrorResponse
from backend.services.discovery_service import WebsiteDiscoveryService
from backend.models.enums import JobStatusEnum, JobTypeEnum
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from backend.models.observation import JobModel

router = APIRouter()


@router.post(
    "/run",
    response_model=DiscoveryJobResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyze Client Website & Discover Offerings",
    description=(
        "Safely crawls the client's provided website URL (with SSRF protection), "
        "extracts products, services, or plans via JSON-LD/OpenGraph/SPA state, "
        "persists new/updated offerings into the client's catalog, auto-discovers dynamic fields, "
        "and updates the discovery Job record."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request or Crawl Error"},
        401: {"model": ErrorResponse, "description": "Authentication Required"},
        402: {"model": ErrorResponse, "description": "Catalog Quota Limit Exceeded"},
        403: {"model": ErrorResponse, "description": "Forbidden - Insufficient Permissions"},
        422: {"model": ErrorResponse, "description": "SSRF / Validation Error"},
    },
)
async def run_website_discovery(
    request: DiscoveryRunRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(require_analyst)],
) -> DiscoveryJobResponse:
    return await WebsiteDiscoveryService.run_discovery(
        db=db,
        client_id=auth_ctx.client_id,
        request=request,
    )


@router.get(
    "/jobs",
    response_model=list[DiscoveryJobResponse],
    status_code=status.HTTP_200_OK,
)
async def list_discovery_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(require_reader)],
) -> list[DiscoveryJobResponse]:
    result = await db.execute(
        select(JobModel)
        .join(SourceModel)
        .join(CompetitorModel)
        .where(
            JobModel.job_type == JobTypeEnum.SCHEMA_DISCOVERY,
            CompetitorModel.client_id == auth_ctx.client_id,
        )
        .order_by(JobModel.created_at.desc())
        .limit(50)
    )
    return [
        await WebsiteDiscoveryService.get_discovery_job(db=db, job_id=job.id, client_id=auth_ctx.client_id)
        for job in result.scalars().all()
    ]


@router.get(
    "/jobs/{job_id}",
    response_model=DiscoveryJobResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Discovery Job Status",
    description="Retrieves the status, metrics, and progress of a discovery job by UUID with strict tenant isolation.",
    responses={
        401: {"model": ErrorResponse, "description": "Authentication Required"},
        403: {"model": ErrorResponse, "description": "Forbidden"},
        404: {"model": ErrorResponse, "description": "Job Not Found"},
    },
)
async def get_discovery_job(
    job_id: Annotated[uuid.UUID, Path(description="Discovery Job UUID", examples=["123e4567-e89b-12d3-a456-426614174000"])],
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(require_reader)],
) -> DiscoveryJobResponse:
    return await WebsiteDiscoveryService.get_discovery_job(
        db=db,
        job_id=job_id,
        client_id=auth_ctx.client_id,
    )
