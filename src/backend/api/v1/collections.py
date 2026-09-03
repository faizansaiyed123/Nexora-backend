from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_db
from backend.models.competitor import OfferingMatchModel
from backend.models.observation import (
    JobModel,
    ObservationModel,
    SnapshotModel,
)
from backend.models.enums import (
    AvailabilityStatusEnum,
    JobStatusEnum,
    JobTypeEnum,
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
    1. Validate offering match.
    2. Create a RUNNING job.
    3. Fetch the target URL.
    4. Create an observation.
    5. Create/update the snapshot.
    6. Mark the job COMPLETED or FAILED.
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

    offering_match_id_val = offering_match.id
    target_url_val = offering_match.target_url
    source_id_val = offering_match.source_id

    # ---------------------------------------------------------
    # 2. Create collection job
    # ---------------------------------------------------------

    job = JobModel(
        source_id=source_id_val,
        job_type=JobTypeEnum.ON_DEMAND_REFRESH,
        status=JobStatusEnum.RUNNING,
        meta_info={
            "offering_match_id": str(offering_match_id_val),
            "target_url": target_url_val,
        },
    )

    session.add(job)
    await session.flush()

    # ---------------------------------------------------------
    # 3. Execute collection
    # ---------------------------------------------------------

    try:
        collector = CollectionService()

        collection_result = await collector.collect(
            target_url_val
        )

        # -----------------------------------------------------
        # 4. Convert availability safely
        # -----------------------------------------------------

        availability_value = (
            collection_result.availability
            or AvailabilityStatusEnum.UNKNOWN.value
        )

        try:
            availability = AvailabilityStatusEnum(
                availability_value
            )
        except ValueError:
            availability = AvailabilityStatusEnum.UNKNOWN

        # -----------------------------------------------------
        # 5. Create observation
        # -----------------------------------------------------

        observation = ObservationModel(
            offering_match_id=offering_match_id_val,
            job_id=job.id,
            observed_price=collection_result.price,
            currency=collection_result.currency or "USD",
            availability=availability,
            response_time_ms=collection_result.response_time_ms,
            http_status_code=collection_result.http_status_code or 0,
            raw_payload=None,
            extracted_attributes=collection_result.attributes,
        )

        session.add(observation)

        await session.flush()

        # -----------------------------------------------------
        # 6. Find existing snapshot
        # -----------------------------------------------------

        snapshot_result = await session.execute(
            select(SnapshotModel).where(
                SnapshotModel.offering_match_id
                == offering_match_id_val
            )
        )

        snapshot = snapshot_result.scalar_one_or_none()

        # -----------------------------------------------------
        # 7. Create first snapshot
        # -----------------------------------------------------

        if snapshot is None:
            snapshot = SnapshotModel(
                offering_match_id=offering_match_id_val,
                current_price=collection_result.price,
                previous_price=None,
                price_difference=None,
                percentage_difference=None,
                current_availability=availability,
                last_observed_at=observation.observed_at,
            )

            session.add(snapshot)

        # -----------------------------------------------------
        # 8. Update existing snapshot
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
        # 9. Update job
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
                collection_result.error or "Collection failed."
            )

        # -----------------------------------------------------
        # 10. Commit everything
        # -----------------------------------------------------

        await session.commit()
        await session.refresh(job)

        # -----------------------------------------------------
        # 11. Return result
        # -----------------------------------------------------

        if not collection_result.success:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "Collection failed.",
                    "error": collection_result.error,
                    "job_id": str(job.id),
                },
            )

        return {
            "job_id": str(job.id),
            "offering_match_id": str(offering_match_id_val),
            "source_id": str(job.source_id),
            "status": job.status,
            "message": "Collection completed successfully.",
            "price": (
                str(collection_result.price)
                if collection_result.price is not None
                else None
            ),
            "currency": collection_result.currency,
            "availability": availability.value,
            "response_time_ms": collection_result.response_time_ms,
            "http_status_code": collection_result.http_status_code,
        }

    # ---------------------------------------------------------
    # Unexpected application/database error
    # ---------------------------------------------------------

    except HTTPException:
        raise

    except Exception as exc:
        await session.rollback()

        # Create a fresh failed job record because rollback
        # removes the uncommitted RUNNING job.
        failed_job = JobModel(
            source_id=source_id_val,
            job_type=JobTypeEnum.ON_DEMAND_REFRESH,
            status=JobStatusEnum.FAILED,
            total_items_processed=1,
            successful_items=0,
            failed_items=1,
            error_message=str(exc),
            meta_info={
                "offering_match_id": str(offering_match_id_val),
                "target_url": target_url_val,
            },
        )

        session.add(failed_job)

        await session.commit()
        await session.refresh(failed_job)

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Collection failed.",
                "error": str(exc),
                "job_id": str(failed_job.id),
            },
        )
