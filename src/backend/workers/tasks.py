from __future__ import annotations

import asyncio
import uuid

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.db.session import AsyncSessionLocal
from backend.models.competitor import OfferingMatchModel, SourceModel
from backend.models.offering import OfferingModel
from backend.models.observation import JobModel
from backend.models.enums import JobStatusEnum, JobTypeEnum
from backend.services.collection_runner import CollectionRunner


async def _enqueue_monitored_match_jobs() -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OfferingMatchModel)
            .options(
                selectinload(OfferingMatchModel.offering),
                selectinload(OfferingMatchModel.source).selectinload(SourceModel.competitor),
            )
            .where(
                OfferingMatchModel.is_active.is_(True),
                OfferingMatchModel.offering.has(
                    (OfferingModel.is_monitored.is_(True))
                    & (OfferingModel.is_archived.is_(False))
                ),
                OfferingMatchModel.source.has(SourceModel.is_active.is_(True)),
            )
        )
        matches = result.scalars().all()
        job_ids: list[str] = []

        for match in matches:
            if match.offering is None or match.source is None:
                continue
            job = JobModel(
                source_id=match.source_id,
                job_type=JobTypeEnum.SCHEDULED_CRAWL,
                status=JobStatusEnum.PENDING,
                meta_info={
                    "offering_match_id": str(match.id),
                    "offering_id": str(match.offering_id),
                    "scheduled": True,
                },
            )
            session.add(job)
            await session.flush()
            job_ids.append(str(job.id))

        await session.commit()
        return job_ids


@shared_task(name="backend.workers.tasks.enqueue_monitored_match_jobs")
def enqueue_monitored_match_jobs() -> dict[str, int]:
    job_ids = asyncio.run(_enqueue_monitored_match_jobs())
    for job_id in job_ids:
        execute_collection_job.delay(job_id)
    return {"scheduled_jobs": len(job_ids)}


@shared_task(
    name="backend.workers.tasks.execute_collection_job",
    bind=True,
    autoretry_for=(ConnectionError,),
    retry_backoff=True,
    max_retries=3,
)
def execute_collection_job(self, job_id: str) -> str:
    async def run_job() -> str:
        async with AsyncSessionLocal() as session:
            job = await CollectionRunner().run(session, uuid.UUID(job_id))
            return job.status.value

    return asyncio.run(run_job())
