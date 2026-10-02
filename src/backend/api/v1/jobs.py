from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import AuthenticatedUserContext, get_current_user_claims, require_analyst
from backend.db.session import get_db
from backend.models.competitor import CompetitorModel, SourceModel
from backend.models.observation import JobModel
from backend.schemas.observation import JobRead

router = APIRouter()


@router.get("", response_model=list[JobRead], dependencies=[Depends(require_reader)])
async def list_jobs(
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    result = await session.execute(
        select(JobModel)
        .join(SourceModel, JobModel.source_id == SourceModel.id)
        .join(CompetitorModel, SourceModel.competitor_id == CompetitorModel.id)
        .where(CompetitorModel.client_id == auth_ctx.client_id)
        .order_by(JobModel.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{job_id}", response_model=JobRead, dependencies=[Depends(require_reader)])
async def get_job(
    job_id: UUID,
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    result = await session.execute(
        select(JobModel)
        .join(SourceModel, JobModel.source_id == SourceModel.id)
        .join(CompetitorModel, SourceModel.competitor_id == CompetitorModel.id)
        .where(
            JobModel.id == job_id,
            CompetitorModel.client_id == auth_ctx.client_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    return job
