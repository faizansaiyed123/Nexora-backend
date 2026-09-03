from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_db
from backend.models.competitor import OfferingMatchModel
from backend.models.observation import JobModel
from backend.models.enums import (
    AvailabilityStatusEnum,
    JobStatusEnum,
    JobTypeEnum,
)
from backend.models.observation import ObservationModel, SnapshotModel
from backend.services.collection_service import CollectionService

router = APIRouter()


@router.post("/run", response_model=dict, status_code=status.HTTP_201_CREATED)
async def run_collection(
    offering_match_id: UUID,
    session: AsyncSession = Depends(get_db),
):
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

    job = JobModel(
        source_id=offering_match.source_id,
        job_type=JobTypeEnum.ON_DEMAND_REFRESH,
        status=JobStatusEnum.RUNNING,
        meta_info={
            "offering_match_id": str(offering_match.id),
            "target_url": offering_match.target_url,
        },
    )

    session.add(job)
    await session.flush()

    try:
        collector = CollectionService()

        result = await collector.collect(
            offering_match.target_url
        )

        observation = ObservationModel(
            offering_match_id=offering_match.id,
            job_id=job.id,
            observed_price=result.price,
            currency="USD",
            availability=AvailabilityStatusEnum(result.availability),
            response_time_ms=result.response_time_ms,
            http_status_code=result.http_status_code,
            raw_payload=result.raw_payload,
            extracted_attributes=result.attributes,
        )

        session.add(observation)

        previous_snapshot_result = await session.execute(
            select(SnapshotModel).where(
                SnapshotModel.offering_match_id == offering_match.id
            )
        )

        snapshot = previous_snapshot_result.scalar_one_or_none()

        if snapshot is None:
            snapshot = SnapshotModel(
                offering_match_id=offering_match.id,
                current_price=result.price,
                previous_price=None,
                price_difference=None,
                percentage_difference=None,
                current_availability=AvailabilityStatusEnum(
                    result.availability
                ),
                last_observed_at=observation.observed_at,
            )

            session.add(snapshot)

        else:
            previous_price = snapshot.current_price

            snapshot.previous_price = previous_price
            snapshot.current_price = result.price

            if previous_price is not None and result.price is not None:
                difference = result.price - previous_price

                snapshot.price_difference = difference

                if previous_price != 0:
                    snapshot.percentage_difference = float(
                        (difference / previous_price) * 100
                    )
                else:
                    snapshot.percentage_difference = None

            snapshot.current_availability = AvailabilityStatusEnum(
                result.availability
            )
            snapshot.last_observed_at = observation.observed_at

        job.status = JobStatusEnum.COMPLETED
        job.total_items_processed = 1
        job.successful_items = 1
        job.failed_items = 0

        await session.commit()
        await session.refresh(job)

        return {
            "job_id": str(job.id),
            "offering_match_id": str(offering_match.id),
            "source_id": str(job.source_id),
            "status": job.status,
            "message": "Collection completed successfully.",
            "price": str(result.price) if result.price is not None else None,
            "availability": result.availability,
            "response_time_ms": result.response_time_ms,
            "http_status_code": result.http_status_code,
        }

    except Exception as exc:
        job.status = JobStatusEnum.FAILED
        job.total_items_processed = 1
        job.successful_items = 0
        job.failed_items = 1
        job.error_message = str(exc)

        await session.commit()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Collection failed: {exc}",
        )
