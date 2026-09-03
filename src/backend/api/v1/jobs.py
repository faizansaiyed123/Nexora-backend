from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_db
from backend.models.observation import JobModel
from backend.schemas.observation import JobRead


router = APIRouter()


@router.get("", response_model=list[JobRead])
async def list_jobs(
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(JobModel).order_by(JobModel.created_at.desc())
    )

    return result.scalars().all()


@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: UUID,
    session: AsyncSession = Depends(get_db),
):
    result = await session.execute(
        select(JobModel).where(JobModel.id == job_id)
    )

    job = result.scalar_one_or_none()

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    return job
