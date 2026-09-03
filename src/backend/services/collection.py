from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_db
from backend.models.competitor import OfferingMatchModel
from backend.models.enums import (
    AvailabilityStatusEnum,
    JobStatusEnum,
    JobTypeEnum,
)
from backend.models.observation import (
    JobModel,
    ObservationModel,
    SnapshotModel,
)
from backend.services.collection_service import CollectionService


router = APIRouter()


@router.post(
    "/run",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
async def run_collection(
    offering_match_id: UUID,
    session: AsyncSession = Depends(get_db),
):
    """
    Collect the current state of an offering match.

    Flow:
    1. Validate the offering match.
    2. Capture required identifiers.
    3. Create a RUNNING job.
    4. Collect the target URL.
    5. Create an observation.
    6. Create or update the snapshot.
    7. Mark the job COMPLETED or FAILED.

    The implementation is source/website independent.
    No website, currency, product, or source is hardcoded.
    """

    # ---------------------------------------------------------
    # 1. Find offering match
    # ---------------------------------------------------------

    result = await session.execute(
        select(OfferingMatchModel).where(
            OfferingMatchModel.id == offering_match_id,
            OfferingMatchModel.is_active.is_(True),
        )
    )

    offering_match = result.scalar_one_or_none()

    if offering_match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering match not found.",
        )

    # ---------------------------------------------------------
    # 2. Capture all values needed later
    #
    # IMPORTANT:
    # Do this before commit/rollback.
    # Async SQLAlchemy must not lazily reload expired ORM
    # attributes after a transaction has ended.
    # ---------------------------------------------------------

    match_id = offering_match.id
    source_id = offering_match.source_id
    target_url = offering_match.target_url

    # ---------------------------------------------------------
    # 3. Create collection job
    # ---------------------------------------------------------

    job = JobModel(
        source_id=source_id,
        job_type=JobTypeEnum.ON_DEMAND_REFRESH,
        status=JobStatusEnum.RUNNING,
        meta_info={
            "offering_match_id": str(match_id),
            "target_url": target_url,
        },
    )

    session.add(job)

    await session.flush()

    # Capture job ID while it is definitely available.
    job_id = job.id

    # ---------------------------------------------------------
    # 4. Execute collection
    # ---------------------------------------------------------

    try:
        collector = CollectionService()

        collection_result = await collector.collect(
            target_url
        )

        # -----------------------------------------------------
        # 5. Normalize availability dynamically
        # -----------------------------------------------------

        availability_value = (
            collection_result.availability
        )

        if availability_value:
            try:
                availability = AvailabilityStatusEnum(
                    availability_value
                )
            except ValueError:
                availability = AvailabilityStatusEnum.UNKNOWN
        else:
            availability = AvailabilityStatusEnum.UNKNOWN

        # -----------------------------------------------------
        # 6. Create observation
        # -----------------------------------------------------

        observation = ObservationModel(
            offering_match_id=match_id,
            job_id=job_id,
            observed_price=collection_result.price,
            currency=collection_result.currency,
            availability=availability,
            response_time_ms=collection_result.response_time_ms,
            http_status_code=collection_result.status_code,
            raw_payload=None,
            extracted_attributes=collection_result.attributes,
        )

        session.add(observation)

        await session.flush()

        # -----------------------------------------------------
        # 7. Find existing snapshot
        # -----------------------------------------------------

        snapshot_result = await session.execute(
            select(SnapshotModel).where(
                SnapshotModel.offering_match_id == match_id
            )
        )

        snapshot = snapshot_result.scalar_one_or_none()

        # -----------------------------------------------------
        # 8. Create first snapshot
        # -----------------------------------------------------

        if snapshot is None:
            snapshot = SnapshotModel(
                offering_match_id=match_id,
                current_price=collection_result.price,
                previous_price=None,
                price_difference=None,
                percentage_difference=None,
                current_availability=availability,
                last_observed_at=observation.observed_at,
            )

            session.add(snapshot)

        # -----------------------------------------------------
        # 9. Update existing snapshot
        # -----------------------------------------------------

        else:
            previous_price = snapshot.current_price
            current_price = collection_result.price

            snapshot.previous_price = previous_price
            snapshot.current_price = current_price

            if (
                previous_price is not None
                and current_price is not None
            ):
                difference = current_price - previous_price

                snapshot.price_difference = difference

                if previous_price != 0:
                    snapshot.percentage_difference = float(
                        (difference / previous_price) * 100
                    )
                else:
                    snapshot.percentage_difference = None

            else:
                snapshot.price_difference = None
                snapshot.percentage_difference = None

            snapshot.current_availability = availability
            snapshot.last_observed_at = observation.observed_at

        # -----------------------------------------------------
        # 10. Update job
        # -----------------------------------------------------

        if collection_result.success:
            job.status = JobStatusEnum.COMPLETED
            job.total_items_processed = 1
            job.successful_items = 1
            job.failed_items = 0
            job.error_category = None
            job.error_message = None

        else:
            job.status = JobStatusEnum.FAILED
            job.total_items_processed = 1
            job.successful_items = 0
            job.failed_items = 1
            job.error_message = (
                collection_result.error
                or "Collection failed."
            )

        # -----------------------------------------------------
        # 11. Commit
        # -----------------------------------------------------

        await session.commit()

        # -----------------------------------------------------
        # 12. Collection failure
        # -----------------------------------------------------

        if not collection_result.success:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "Collection failed.",
                    "error": collection_result.error,
                    "job_id": str(job_id),
                },
            )

        # -----------------------------------------------------
        # 13. Return successful result
        # -----------------------------------------------------

        return {
            "job_id": str(job_id),
            "offering_match_id": str(match_id),
            "source_id": str(source_id),
            "status": JobStatusEnum.COMPLETED,
            "message": "Collection completed successfully.",
            "price": (
                str(collection_result.price)
                if collection_result.price is not None
                else None
            ),
            "currency": collection_result.currency,
            "availability": availability.value,
            "response_time_ms": collection_result.response_time_ms,
            "http_status_code": collection_result.status_code,
        }

    # ---------------------------------------------------------
    # Expected HTTP error
    # ---------------------------------------------------------

    except HTTPException:
        raise

    # ---------------------------------------------------------
    # Unexpected error
    # ---------------------------------------------------------

    except Exception as exc:
        await session.rollback()

        # -----------------------------------------------------
        # Create a new FAILED job.
        #
        # IMPORTANT:
        # Use the previously captured source_id, match_id and
        # target_url instead of accessing offering_match again.
        # This prevents MissingGreenlet caused by SQLAlchemy
        # trying to lazily reload expired attributes.
        # -----------------------------------------------------

        failed_job = JobModel(
            source_id=source_id,
            job_type=JobTypeEnum.ON_DEMAND_REFRESH,
            status=JobStatusEnum.FAILED,
            total_items_processed=1,
            successful_items=0,
            failed_items=1,
            error_message=str(exc),
            meta_info={
                "offering_match_id": str(match_id),
                "target_url": target_url,
            },
        )

        session.add(failed_job)

        await session.commit()

        failed_job_id = failed_job.id

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Collection failed.",
                "error": str(exc),
                "job_id": str(failed_job_id),
            },
        )
