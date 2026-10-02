from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.enums import (
    AvailabilityStatusEnum,
    ErrorCategoryEnum,
    JobStatusEnum,
)
from backend.models.observation import (
    JobModel,
    ObservationModel,
    SnapshotModel,
)
from backend.models.competitor import OfferingMatchModel

from backend.services.collection_service import CollectionService


class CollectionRunner:
    """
    Executes a collection job and persists the result.

    Flow:

        Job
          ↓
        OfferingMatch
          ↓
        CollectionService
          ↓
        Observation
          ↓
        Snapshot
          ↓
        Job COMPLETED / FAILED
    """

    def __init__(self) -> None:
        self.collector = CollectionService()

    async def run(
        self,
        session: AsyncSession,
        job_id: UUID,
    ) -> JobModel:
        """
        Execute a pending collection job.
        """

        # ---------------------------------------------------------
        # 1. Load job
        # ---------------------------------------------------------

        result = await session.execute(
            select(JobModel).where(
                JobModel.id == job_id
            )
        )

        job = result.scalar_one_or_none()

        if job is None:
            raise ValueError("Collection job not found.")

        if job.status not in {
            JobStatusEnum.PENDING,
            JobStatusEnum.RUNNING,
        }:
            return job

        # ---------------------------------------------------------
        # 2. Mark job as running
        # ---------------------------------------------------------

        job.status = JobStatusEnum.RUNNING
        job.started_at = datetime.now(timezone.utc)

        await session.commit()
        await session.refresh(job)

        try:
            # -----------------------------------------------------
            # 3. Get offering match
            # -----------------------------------------------------

            offering_match_id = job.meta_info.get(
                "offering_match_id"
            )

            if not offering_match_id:
                raise ValueError(
                    "Job is missing offering_match_id."
                )

            offering_match_uuid = UUID(
                str(offering_match_id)
            )

            result = await session.execute(
                select(OfferingMatchModel).where(
                    OfferingMatchModel.id
                    == offering_match_uuid,
                    OfferingMatchModel.is_active.is_(True),
                )
            )

            offering_match = result.scalar_one_or_none()

            if offering_match is None:
                raise ValueError(
                    "Offering match not found."
                )

            # -----------------------------------------------------
            # 4. Get target URL
            # -----------------------------------------------------

            target_url = offering_match.target_url

            if not target_url:
                raise ValueError(
                    "Offering match has no target URL."
                )

            # -----------------------------------------------------
            # 5. Collect the website
            # -----------------------------------------------------

            collection_result = await self.collector.collect(
                url=target_url
            )

            # -----------------------------------------------------
            # 6. Convert availability
            # -----------------------------------------------------

            availability = (
                self._availability_enum(
                    collection_result.availability
                )
            )

            # -----------------------------------------------------
            # 7. Create observation
            # -----------------------------------------------------

            observation = ObservationModel(
                offering_match_id=offering_match.id,
                job_id=job.id,
                observed_price=collection_result.price,
                currency=(
                    collection_result.currency
                    or "USD"
                ),
                availability=availability,
                response_time_ms=(
                    collection_result.response_time_ms
                ),
                http_status_code=(
                    collection_result.status_code
                    or 0
                ),
                raw_payload=None,
                extracted_attributes=(
                    collection_result.attributes
                ),
                observed_at=datetime.now(timezone.utc),
            )

            session.add(observation)

            # -----------------------------------------------------
            # 8. Update job counters
            # -----------------------------------------------------

            job.total_items_processed = 1

            if collection_result.success:
                job.successful_items = 1
                job.failed_items = 0
            else:
                job.successful_items = 0
                job.failed_items = 1

            # -----------------------------------------------------
            # 9. If collection failed
            # -----------------------------------------------------

            if not collection_result.success:
                job.status = JobStatusEnum.FAILED
                job.error_category = (
                    self._error_category(
                        collection_result.error
                    )
                )
                job.error_message = (
                    collection_result.error
                )
                job.completed_at = (
                    datetime.now(timezone.utc)
                )

                await session.commit()
                await session.refresh(job)

                return job

            # -----------------------------------------------------
            # 10. Flush observation so it exists in DB
            # -----------------------------------------------------

            await session.flush()

            # -----------------------------------------------------
            # 11. Get previous snapshot
            # -----------------------------------------------------

            result = await session.execute(
                select(SnapshotModel).where(
                    SnapshotModel.offering_match_id
                    == offering_match.id
                )
            )

            snapshot = result.scalar_one_or_none()

            previous_price: Optional[float] = None

            if snapshot is not None:
                previous_price = snapshot.current_price

            current_price = collection_result.price

            # -----------------------------------------------------
            # 12. Calculate price changes
            # -----------------------------------------------------

            price_difference = None
            percentage_difference = None

            if (
                previous_price is not None
                and current_price is not None
            ):
                price_difference = (
                    current_price
                    - previous_price
                )

                if previous_price != 0:
                    percentage_difference = float(
                        (
                            price_difference
                            / previous_price
                        )
                        * 100
                    )

            # -----------------------------------------------------
            # 13. Create/update snapshot
            # -----------------------------------------------------

            if snapshot is None:

                snapshot = SnapshotModel(
                    offering_match_id=(
                        offering_match.id
                    ),
                    current_price=current_price,
                    previous_price=None,
                    price_difference=None,
                    percentage_difference=None,
                    current_availability=availability,
                    last_observed_at=(
                        observation.observed_at
                    ),
                    updated_at=(
                        datetime.now(timezone.utc)
                    ),
                )

                session.add(snapshot)

            else:

                snapshot.previous_price = (
                    snapshot.current_price
                )

                snapshot.current_price = (
                    current_price
                )

                snapshot.price_difference = (
                    price_difference
                )

                snapshot.percentage_difference = (
                    percentage_difference
                )

                snapshot.current_availability = (
                    availability
                )

                snapshot.last_observed_at = (
                    observation.observed_at
                )

                snapshot.updated_at = (
                    datetime.now(timezone.utc)
                )

            # -----------------------------------------------------
            # 14. Complete job
            # -----------------------------------------------------

            job.status = JobStatusEnum.COMPLETED
            job.completed_at = (
                datetime.now(timezone.utc)
            )
            job.error_category = None
            job.error_message = None

            await session.commit()
            await session.refresh(job)

            return job

        except Exception as exc:

            # -----------------------------------------------------
            # 15. Fail job safely
            # -----------------------------------------------------

            await session.rollback()

            # Re-load job after rollback
            result = await session.execute(
                select(JobModel).where(
                    JobModel.id == job_id
                )
            )

            job = result.scalar_one()

            job.status = JobStatusEnum.FAILED
            job.total_items_processed = 1
            job.successful_items = 0
            job.failed_items = 1
            job.error_category = (
                self._error_category(
                    str(exc)
                )
            )
            job.error_message = str(exc)
            job.completed_at = (
                datetime.now(timezone.utc)
            )

            await session.commit()
            await session.refresh(job)

            return job

    @staticmethod
    def _availability_enum(
        value: Optional[str],
    ) -> AvailabilityStatusEnum:
        """
        Convert collector availability into
        the database enum.
        """

        if not value:
            return AvailabilityStatusEnum.UNKNOWN

        try:
            return AvailabilityStatusEnum(value)
        except ValueError:
            return AvailabilityStatusEnum.UNKNOWN

    @staticmethod
    def _error_category(
        error: Optional[str],
    ) -> ErrorCategoryEnum:
        """
        Convert a collection error into
        the Nexora error category.
        """

        if not error:
            return ErrorCategoryEnum.PARSING_ERROR

        text = error.lower()

        if "timeout" in text:
            return ErrorCategoryEnum.NETWORK_TIMEOUT

        if "http 4" in text:
            return ErrorCategoryEnum.HTTP_4XX

        if "http 5" in text:
            return ErrorCategoryEnum.HTTP_5XX

        return ErrorCategoryEnum.PARSING_ERROR
