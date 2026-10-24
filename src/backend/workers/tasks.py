from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

from celery import shared_task
from celery.signals import worker_process_shutdown, worker_shutdown
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from backend.db.session import AsyncSessionLocal, engine
from backend.core.config import get_settings
from backend.models.competitor import OfferingMatchModel, SourceModel
from backend.models.offering import OfferingModel
from backend.models.observation import JobModel
from backend.models.notification import NotificationOutboxModel
from backend.models.enums import JobStatusEnum, JobTypeEnum
from backend.services.collection_runner import CollectionRunner
from backend.workers.email_tasks import send_competitive_alert_email_async

settings = get_settings()


# SQLAlchemy's asyncpg connection pool is bound to the event loop that created
# each connection. Calling asyncio.run() for every Celery task closes that loop,
# leaving pooled connections attached to a dead loop for the next task. A worker
# process uses one long-lived loop for all its async database work.
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_loop_pid: int | None = None


def _run_worker_async(coroutine):
    global _worker_loop, _worker_loop_pid
    pid = os.getpid()
    if (
        _worker_loop is None
        or _worker_loop.is_closed()
        or _worker_loop_pid != pid
    ):
        _worker_loop = asyncio.new_event_loop()
        _worker_loop_pid = pid
    asyncio.set_event_loop(_worker_loop)
    return _worker_loop.run_until_complete(coroutine)


def _close_worker_async_loop(**_kwargs) -> None:
    global _worker_loop, _worker_loop_pid
    loop = _worker_loop
    if loop is None or loop.is_closed():
        _worker_loop = None
        _worker_loop_pid = None
        return
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(engine.dispose())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
        _worker_loop = None
        _worker_loop_pid = None


worker_process_shutdown.connect(_close_worker_async_loop, weak=False)
worker_shutdown.connect(_close_worker_async_loop, weak=False)


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

        now = datetime.now(timezone.utc)
        await session.execute(
            update(JobModel)
            .where(
                JobModel.status == JobStatusEnum.RUNNING,
                or_(
                    (
                        JobModel.lease_expires_at.is_not(None)
                        & (JobModel.lease_expires_at < now)
                    ),
                    (
                        JobModel.lease_expires_at.is_(None)
                        & JobModel.started_at.is_not(None)
                        & (
                            JobModel.started_at
                            < now - timedelta(seconds=settings.job_lease_seconds)
                        )
                    ),
                ),
            )
            .values(
                status=JobStatusEnum.FAILED,
                error_message="Collection lease expired before completion.",
                failed_items=1,
                total_items_processed=1,
                completed_at=now,
                lease_expires_at=None,
            )
        )

        for match in matches:
            if match.offering is None or match.source is None:
                continue
            stmt = (
                pg_insert(JobModel)
                .values(
                    source_id=match.source_id,
                    job_type=JobTypeEnum.SCHEDULED_CRAWL,
                    status=JobStatusEnum.PENDING,
                    meta_info={
                        "offering_match_id": str(match.id),
                        "offering_id": str(match.offering_id),
                        "scheduled": True,
                    },
                )
                .on_conflict_do_nothing()
                .returning(JobModel.id)
            )
            inserted_id = await session.scalar(stmt)
            if inserted_id is not None:
                job_ids.append(str(inserted_id))

        await session.commit()
        return job_ids


@shared_task(name="backend.workers.tasks.enqueue_monitored_match_jobs")
def enqueue_monitored_match_jobs() -> dict[str, int]:
    job_ids = _run_worker_async(_enqueue_monitored_match_jobs())
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

    return _run_worker_async(run_job())


async def _dispatch_pending_notifications(batch_size: int = 25) -> int:
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=10)
    sent = 0

    for _ in range(batch_size):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(NotificationOutboxModel)
                .where(
                    (
                        (NotificationOutboxModel.status == "PENDING")
                        & (NotificationOutboxModel.next_attempt_at <= now)
                    )
                    | (
                        (NotificationOutboxModel.status == "SENDING")
                        & (NotificationOutboxModel.locked_at.is_not(None))
                        & (NotificationOutboxModel.locked_at < stale_before)
                    )
                )
                .order_by(NotificationOutboxModel.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            notification = result.scalar_one_or_none()
            if notification is None:
                await session.rollback()
                break

            notification.status = "SENDING"
            notification.locked_at = now
            notification.attempts += 1
            await session.commit()

            try:
                if notification.channel != "email":
                    raise RuntimeError("Unsupported notification channel.")
                await send_competitive_alert_email_async(
                    notification.recipient,
                    notification.title,
                    notification.message,
                )
            except Exception as exc:
                retry_at = now + timedelta(minutes=min(60, 2 ** min(notification.attempts, 6)))
                async with AsyncSessionLocal() as update_session:
                    await update_session.execute(
                        update(NotificationOutboxModel)
                        .where(
                            NotificationOutboxModel.id == notification.id,
                            NotificationOutboxModel.status == "SENDING",
                        )
                        .values(
                            status="PENDING",
                            locked_at=None,
                            last_error=f"{type(exc).__name__}: notification delivery failed",
                            next_attempt_at=retry_at,
                        )
                    )
                    await update_session.commit()
            else:
                async with AsyncSessionLocal() as update_session:
                    await update_session.execute(
                        update(NotificationOutboxModel)
                        .where(
                            NotificationOutboxModel.id == notification.id,
                            NotificationOutboxModel.status == "SENDING",
                        )
                        .values(
                            status="SENT",
                            locked_at=None,
                            sent_at=datetime.now(timezone.utc),
                            last_error=None,
                        )
                    )
                    await update_session.commit()
                sent += 1
    return sent


@shared_task(name="backend.workers.tasks.dispatch_pending_notifications")
def dispatch_pending_notifications() -> dict[str, int]:
    return {"sent": _run_worker_async(_dispatch_pending_notifications())}
