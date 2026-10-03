from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from backend.core.config import get_settings
from backend.db.session import AsyncSessionLocal
from backend.models.alert import NotificationOutboxModel
from backend.models.competitor import OfferingMatchModel, SourceModel
from backend.models.enums import JobStatusEnum, JobTypeEnum
from backend.models.offering import OfferingModel
from backend.models.observation import JobModel
from backend.services.collection_runner import CollectionRunner
from backend.workers.email_tasks import send_outbox_email_async

logger = logging.getLogger("nexora.workers")


async def _recover_stale_collection_jobs(session) -> int:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.collection_job_lease_minutes)
    stmt = (
        update(JobModel)
        .where(
            JobModel.status == JobStatusEnum.RUNNING,
            JobModel.job_type != JobTypeEnum.SCHEMA_DISCOVERY,
            (
                (JobModel.lease_expires_at.is_not(None) & (JobModel.lease_expires_at < now))
                | (
                    JobModel.lease_expires_at.is_(None)
                    & JobModel.started_at.is_not(None)
                    & (JobModel.started_at < cutoff)
                )
            ),
        )
        .values(
            status=JobStatusEnum.FAILED,
            started_at=JobModel.started_at,
            completed_at=now,
            lease_expires_at=None,
            error_message="Collection worker lease expired.",
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def _enqueue_monitored_match_jobs() -> list[str]:
    async with AsyncSessionLocal() as session:
        await _recover_stale_collection_jobs(session)

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

        now = datetime.now(timezone.utc)
        interval_seconds = max(60, get_settings().scheduled_collection_interval_minutes * 60)
        bucket = int(now.timestamp() // interval_seconds)

        for match in matches:
            if match.offering is None or match.source is None:
                continue

            active = await session.scalar(
                select(JobModel.id)
                .where(
                    JobModel.source_id == match.source_id,
                    JobModel.job_type == JobTypeEnum.SCHEDULED_CRAWL,
                    JobModel.status.in_([JobStatusEnum.PENDING, JobStatusEnum.RUNNING]),
                    JobModel.meta_info["offering_match_id"].as_string() == str(match.id),
                )
                .limit(1)
            )
            if active is not None:
                continue

            idempotency_key = f"scheduled:{match.id}:{bucket}"
            stmt = (
                pg_insert(JobModel)
                .values(
                    source_id=match.source_id,
                    job_type=JobTypeEnum.SCHEDULED_CRAWL,
                    status=JobStatusEnum.PENDING,
                    idempotency_key=idempotency_key,
                    attempt_count=0,
                    meta_info={
                        "offering_match_id": str(match.id),
                        "offering_id": str(match.offering_id),
                        "scheduled": True,
                        "schedule_bucket": bucket,
                    },
                )
                .on_conflict_do_nothing(index_elements=[JobModel.idempotency_key])
                .returning(JobModel.id)
            )
            created_id = await session.scalar(stmt)
            if created_id is not None:
                job_ids.append(str(created_id))

        await session.commit()
        return job_ids


@shared_task(name="backend.workers.tasks.enqueue_monitored_match_jobs")
def enqueue_monitored_match_jobs() -> dict[str, int]:
    job_ids = asyncio.run(_enqueue_monitored_match_jobs())
    for job_id in job_ids:
        execute_collection_job.delay(job_id)
    return {"scheduled_jobs": len(job_ids)}


async def _claim_notification(session) -> NotificationOutboxModel | None:
    now = datetime.now(timezone.utc)
    stale = (
        update(NotificationOutboxModel)
        .where(
            NotificationOutboxModel.status == "PROCESSING",
            NotificationOutboxModel.locked_until.is_not(None),
            NotificationOutboxModel.locked_until < now,
        )
        .values(status="PENDING", locked_until=None)
    )
    await session.execute(stale)

    row = await session.scalar(
        select(NotificationOutboxModel)
        .where(
            NotificationOutboxModel.status == "PENDING",
            NotificationOutboxModel.next_attempt_at <= now,
        )
        .order_by(NotificationOutboxModel.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        await session.commit()
        return None

    row.status = "PROCESSING"
    row.attempt_count += 1
    row.locked_until = now + timedelta(minutes=10)
    await session.commit()
    return row


async def _process_notification_outbox() -> int:
    processed = 0
    settings = get_settings()

    while processed < 50:
        async with AsyncSessionLocal() as session:
            item = await _claim_notification(session)

        if item is None:
            break

        try:
            if item.channel == "EMAIL":
                await send_outbox_email_async(
                    item.recipient,
                    item.subject,
                    item.body,
                    message_id=item.dedupe_key,
                )
            else:
                raise RuntimeError("Unsupported notification channel")

            async with AsyncSessionLocal() as session:
                row = await session.get(NotificationOutboxModel, item.id)
                if row is not None:
                    row.status = "SENT"
                    row.sent_at = datetime.now(timezone.utc)
                    row.locked_until = None
                    row.last_error = None
                    await session.commit()
        except Exception:
            logger.exception("Notification delivery failed for outbox item %s", item.id)
            async with AsyncSessionLocal() as session:
                row = await session.get(NotificationOutboxModel, item.id)
                if row is not None:
                    row.locked_until = None
                    if row.attempt_count >= settings.notification_outbox_max_attempts:
                        row.status = "FAILED"
                        row.last_error = "Notification delivery failed."
                    else:
                        row.status = "PENDING"
                        row.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                            seconds=min(3600, 30 * (2 ** max(0, row.attempt_count - 1)))
                        )
                        row.last_error = "Notification delivery failed."
                    await session.commit()

        processed += 1

    return processed


@shared_task(name="backend.workers.tasks.process_notification_outbox")
def process_notification_outbox() -> dict[str, int]:
    return {"processed": asyncio.run(_process_notification_outbox())}


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
