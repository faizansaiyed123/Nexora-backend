from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.collectors.browser import render_page
from backend.core.config import get_settings
from backend.core.error_messages import safe_collection_error
from backend.core.rate_limit import DistributedRateLimiter
from backend.models.competitor import OfferingMatchModel, SourceModel
from backend.models.enums import AvailabilityStatusEnum, ErrorCategoryEnum, JobStatusEnum
from backend.models.observation import JobModel, ObservationModel, SnapshotModel
from backend.services.alert_service import AlertService
from backend.services.collection_service import CollectionService
from backend.services.source_service import SourceService

settings = get_settings()


class CollectionRunner:
    """Shared persistence path for scheduled/background collection jobs.

    A job is leased atomically before any external work starts. The lease token
    acts as a lightweight fencing token so an obsolete worker cannot finalize a
    job after it has been safely reclaimed.
    """

    def __init__(self) -> None:
        self.collector = CollectionService()

    async def _claim_job(self, session: AsyncSession, job_id: UUID) -> tuple[JobModel | None, UUID | None]:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=settings.job_lease_seconds)
        lease_token = uuid4()

        stale_running = and_(
            JobModel.status == JobStatusEnum.RUNNING,
            or_(
                JobModel.lease_expires_at < now,
                and_(
                    JobModel.lease_expires_at.is_(None),
                    JobModel.started_at.is_not(None),
                    JobModel.started_at < now - timedelta(seconds=settings.job_lease_seconds),
                ),
            ),
        )
        claim = await session.execute(
            update(JobModel)
            .where(
                JobModel.id == job_id,
                or_(JobModel.status == JobStatusEnum.PENDING, stale_running),
            )
            .values(
                status=JobStatusEnum.RUNNING,
                started_at=now,
                completed_at=None,
                lease_expires_at=lease_until,
                lease_token=lease_token,
                error_category=None,
                error_message=None,
            )
            .returning(JobModel.id)
        )
        claimed_id = claim.scalar_one_or_none()
        if claimed_id is None:
            existing = await session.scalar(select(JobModel).where(JobModel.id == job_id))
            await session.rollback()
            return existing, None
        await session.commit()
        job = await session.scalar(select(JobModel).where(JobModel.id == job_id))
        return job, lease_token

    async def _owns_lease(self, session: AsyncSession, job_id: UUID, lease_token: UUID) -> bool:
        current = await session.scalar(
            select(JobModel.lease_token)
            .where(JobModel.id == job_id)
            .with_for_update()
        )
        return current == lease_token

    async def run(self, session: AsyncSession, job_id: UUID) -> JobModel:
        job, lease_token = await self._claim_job(session, job_id)
        if job is None:
            raise ValueError("Collection job not found.")
        if lease_token is None:
            return job

        try:
            match_id = (job.meta_info or {}).get("offering_match_id")
            if not match_id:
                raise ValueError("Job is missing offering_match_id.")

            match = await session.scalar(
                select(OfferingMatchModel)
                .options(
                    selectinload(OfferingMatchModel.offering),
                    selectinload(OfferingMatchModel.source)
                    .selectinload(SourceModel.competitor),
                    selectinload(OfferingMatchModel.source)
                    .selectinload(SourceModel.configuration),
                )
                .where(
                    OfferingMatchModel.id == UUID(str(match_id)),
                    OfferingMatchModel.is_active.is_(True),
                )
            )
            if match is None or match.offering is None or match.source is None or match.source.competitor is None:
                raise ValueError("Offering match not found.")

            offering = match.offering
            source = match.source
            competitor = source.competitor

            if not SourceService.before_collection(source):
                raise ValueError("Collection source is inactive or temporarily unavailable.")

            config = source.configuration
            timeout = float(config.timeout_seconds) if config else self.collector.DEFAULT_TIMEOUT
            headers = config.custom_headers if config and isinstance(config.custom_headers, dict) else None
            selectors = config.extraction_selectors if config and isinstance(config.extraction_selectors, dict) else None

            if config:
                await DistributedRateLimiter.check_rate_limit(
                    "source",
                    str(source.id),
                    max_requests=config.rate_limit_rpm,
                    window_seconds=60,
                )

            result = await self.collector.collect(
                match.target_url,
                timeout=timeout,
                headers=headers,
                custom_selectors=selectors,
                max_retries=config.max_retries if config else 0,
            )

            if config and config.requires_javascript:
                rendered = await render_page(
                    match.target_url,
                    timeout_seconds=config.timeout_seconds,
                )
                if rendered:
                    result = self.collector.from_rendered_html(
                        rendered,
                        match.target_url,
                        response_time_ms=result.response_time_ms,
                        status_code=result.status_code or 200,
                        custom_selectors=selectors,
                    )

            availability = self._availability(result.availability)

            # Serialize the state transition for exactly this match. The network
            # request remains outside the lock, so unrelated collections are not
            # blocked by slow remote sites.
            await session.execute(
                select(OfferingMatchModel.id)
                .where(OfferingMatchModel.id == match.id)
                .with_for_update()
            )
            previous_snapshot = await session.scalar(
                select(SnapshotModel)
                .where(SnapshotModel.offering_match_id == match.id)
                .with_for_update()
            )
            previous_price = previous_snapshot.current_price if previous_snapshot else None
            previous_availability = previous_snapshot.current_availability if previous_snapshot else None

            observation = ObservationModel(
                offering_match_id=match.id,
                job_id=job.id,
                observed_price=result.price,
                currency=result.currency or "USD",
                availability=availability,
                response_time_ms=result.response_time_ms,
                http_status_code=result.status_code or 0,
                extracted_attributes=result.attributes or {},
                observed_at=datetime.now(timezone.utc),
            )
            session.add(observation)
            job.total_items_processed = 1

            if not result.success:
                circuit_tripped = SourceService.record_collection_failure(source)
                job.status = JobStatusEnum.FAILED
                job.successful_items = 0
                job.failed_items = 1
                job.error_category = self._error_category(result.error)
                job.error_message = result.error or "Collection failed."
                job.completed_at = datetime.now(timezone.utc)
                job.lease_expires_at = None

                if circuit_tripped:
                    await AlertService.evaluate_and_trigger(
                        session,
                        client_id=offering.client_id,
                        offering_id=offering.id,
                        offering_match_id=match.id,
                        offering_name=offering.name,
                        competitor_name=competitor.name,
                        source_name=source.name,
                        client_price=offering.base_price,
                        previous_price=previous_price,
                        current_price=None,
                        previous_availability=previous_availability,
                        current_availability=availability,
                        percentage_difference=None,
                        circuit_tripped=True,
                    )
                if not await self._owns_lease(session, job_id, lease_token):
                    await session.rollback()
                    return await session.scalar(select(JobModel).where(JobModel.id == job_id))
                await session.commit()
                await session.refresh(job)
                return job

            SourceService.record_collection_success(source)
            current_price = result.price
            price_difference: Optional[Decimal] = None
            percentage_difference: Optional[float] = None

            if previous_price is not None and current_price is not None:
                price_difference = current_price - previous_price
                if previous_price != 0:
                    percentage_difference = float((price_difference / previous_price) * 100)

            if previous_snapshot is None:
                session.add(
                    SnapshotModel(
                        offering_match_id=match.id,
                        current_price=current_price,
                        previous_price=None,
                        price_difference=None,
                        percentage_difference=None,
                        current_availability=availability,
                        last_observed_at=observation.observed_at,
                    )
                )
            else:
                previous_snapshot.previous_price = previous_snapshot.current_price
                if current_price is not None:
                    previous_snapshot.current_price = current_price
                    previous_snapshot.price_difference = price_difference
                    previous_snapshot.percentage_difference = percentage_difference
                else:
                    previous_snapshot.price_difference = None
                    previous_snapshot.percentage_difference = None
                previous_snapshot.current_availability = availability
                previous_snapshot.last_observed_at = observation.observed_at

            await session.flush()

            await AlertService.evaluate_and_trigger(
                session,
                client_id=offering.client_id,
                offering_id=offering.id,
                offering_match_id=match.id,
                offering_name=offering.name,
                competitor_name=competitor.name,
                source_name=source.name,
                client_price=offering.base_price,
                previous_price=previous_price,
                current_price=current_price,
                previous_availability=previous_availability,
                current_availability=availability,
                percentage_difference=percentage_difference,
                circuit_tripped=False,
            )

            job.status = JobStatusEnum.COMPLETED
            job.successful_items = 1
            job.failed_items = 0
            job.error_category = None
            job.error_message = None
            job.completed_at = datetime.now(timezone.utc)
            job.lease_expires_at = None

            if not await self._owns_lease(session, job_id, lease_token):
                await session.rollback()
                return await session.scalar(select(JobModel).where(JobModel.id == job_id))

            await session.commit()
            await session.refresh(job)
            return job

        except Exception as exc:
            await session.rollback()
            logger = __import__("logging").getLogger("nexora.collection_runner")
            logger.exception("Collection job %s failed unexpectedly: %s", job_id, exc)

            current = await session.scalar(select(JobModel).where(JobModel.id == job_id))
            if current is None:
                raise
            if current.lease_token != lease_token:
                return current

            current.status = JobStatusEnum.FAILED
            current.total_items_processed = 1
            current.successful_items = 0
            current.failed_items = 1
            current.error_category = self._error_category(type(exc).__name__)
            current.error_message = safe_collection_error(exc)
            current.completed_at = datetime.now(timezone.utc)
            current.lease_expires_at = None
            await session.commit()
            await session.refresh(current)
            return current

    @staticmethod
    def _availability(value: Optional[str]) -> AvailabilityStatusEnum:
        if not value:
            return AvailabilityStatusEnum.UNKNOWN
        try:
            return AvailabilityStatusEnum(value)
        except ValueError:
            return AvailabilityStatusEnum.UNKNOWN

    @staticmethod
    def _error_category(error: Optional[str]) -> ErrorCategoryEnum:
        if not error:
            return ErrorCategoryEnum.PARSING_ERROR
        text = error.lower()
        if "timeout" in text:
            return ErrorCategoryEnum.NETWORK_TIMEOUT
        if "http 4" in text:
            return ErrorCategoryEnum.HTTP_4XX
        if "http 5" in text:
            return ErrorCategoryEnum.HTTP_5XX
        if "circuit" in text:
            return ErrorCategoryEnum.CIRCUIT_BREAKER_OPEN
        return ErrorCategoryEnum.PARSING_ERROR
