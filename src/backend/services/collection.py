from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.db.session import get_db

from backend.models.competitor import (
    OfferingMatchModel,
    SourceModel,
)

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

from backend.services.collection_service import (
    CollectionService,
)


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
    Collect current competitive intelligence.

    Flow:

        1. Validate offering match.
        2. Create collection job.
        3. Fetch target URL.
        4. Dynamically extract data.
        5. Create observation.
        6. Create/update snapshot.
        7. Calculate competitive comparison.
        8. Return detailed response.
    """

    # =========================================================
    # 1. FIND OFFERING MATCH
    # =========================================================

    result = await session.execute(
        select(OfferingMatchModel)
        .options(
            selectinload(
                OfferingMatchModel.offering
            ),
            selectinload(
                OfferingMatchModel.source
            ).selectinload(
                SourceModel.competitor
            ),
        )
        .where(
            OfferingMatchModel.id
            == offering_match_id,
            OfferingMatchModel.is_active.is_(True),
        )
    )

    offering_match = (
        result.scalar_one_or_none()
    )

    if offering_match is None:

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering match not found.",
        )

    offering_match_id_val = (
        offering_match.id
    )

    target_url_val = (
        offering_match.target_url
    )

    source_id_val = (
        offering_match.source_id
    )

    offering = (
        offering_match.offering
    )

    source = (
        offering_match.source
    )

    competitor = (
        source.competitor
        if source
        else None
    )

    # =========================================================
    # 2. CREATE JOB
    # =========================================================

    job = JobModel(
        source_id=source_id_val,
        job_type=(
            JobTypeEnum.ON_DEMAND_REFRESH
        ),
        status=JobStatusEnum.RUNNING,
        meta_info={
            "offering_match_id": str(
                offering_match_id_val
            ),
            "target_url": target_url_val,
            "offering_id": (
                str(offering.id)
                if offering
                else None
            ),
        },
    )

    session.add(job)

    await session.flush()

    # =========================================================
    # 3. COLLECTION
    # =========================================================

    try:

        collector = CollectionService()

        collection_result = (
            await collector.collect(
                target_url_val
            )
        )

        # =====================================================
        # 4. NORMALIZE AVAILABILITY
        # =====================================================

        availability_value = (
            collection_result.availability
            or AvailabilityStatusEnum.UNKNOWN.value
        )

        try:

            availability = (
                AvailabilityStatusEnum(
                    availability_value
                )
            )

        except ValueError:

            availability = (
                AvailabilityStatusEnum.UNKNOWN
            )

        # =====================================================
        # 5. CREATE OBSERVATION
        # =====================================================

        observation = ObservationModel(
            offering_match_id=(
                offering_match_id_val
            ),

            job_id=job.id,

            observed_price=(
                collection_result.price
            ),

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

            # Keep this if your DB column is intended
            # to store raw response data.
            #
            # If you don't want to store full HTML,
            # change this back to None.
            raw_payload=(
                collection_result.raw_payload
            ),

            extracted_attributes=(
                collection_result.attributes
            ),
        )

        session.add(
            observation
        )

        await session.flush()

        # =====================================================
        # 6. FIND SNAPSHOT
        # =====================================================

        snapshot_result = await session.execute(
            select(SnapshotModel).where(
                SnapshotModel.offering_match_id
                == offering_match_id_val
            )
        )

        snapshot = (
            snapshot_result.scalar_one_or_none()
        )

        # =====================================================
        # 7. SNAPSHOT
        # =====================================================

        price_movement = (
            "FIRST_OBSERVATION"
        )

        if snapshot is None:

            snapshot = SnapshotModel(
                offering_match_id=(
                    offering_match_id_val
                ),

                current_price=(
                    collection_result.price
                ),

                previous_price=None,

                price_difference=None,

                percentage_difference=None,

                current_availability=(
                    availability
                ),

                last_observed_at=(
                    observation.observed_at
                ),
            )

            session.add(
                snapshot
            )

        else:

            previous_price = (
                snapshot.current_price
            )

            current_price = (
                collection_result.price
            )

            snapshot.previous_price = (
                previous_price
            )

            snapshot.current_price = (
                current_price
            )

            if (
                previous_price is not None
                and current_price is not None
            ):

                difference = (
                    current_price
                    - previous_price
                )

                snapshot.price_difference = (
                    difference
                )

                if previous_price != 0:

                    snapshot.percentage_difference = (
                        float(
                            (
                                difference
                                / previous_price
                            )
                            * 100
                        )
                    )

                else:

                    snapshot.percentage_difference = (
                        None
                    )

                if difference > 0:

                    price_movement = (
                        "INCREASE"
                    )

                elif difference < 0:

                    price_movement = (
                        "DECREASE"
                    )

                else:

                    price_movement = (
                        "UNCHANGED"
                    )

            else:

                snapshot.price_difference = (
                    None
                )

                snapshot.percentage_difference = (
                    None
                )

                price_movement = (
                    "FIRST_OBSERVATION"
                )

            snapshot.current_availability = (
                availability
            )

            snapshot.last_observed_at = (
                observation.observed_at
            )

        # =====================================================
        # 8. COMPETITIVE COMPARISON
        # =====================================================

        client_price_info = None

        if (
            offering
            and offering.current_price is not None
            and collection_result.price is not None
        ):

            client_price = float(
                offering.current_price
            )

            competitor_price = float(
                collection_result.price
            )

            difference = (
                competitor_price
                - client_price
            )

            percentage_difference = (
                (
                    difference
                    / client_price
                )
                * 100
                if client_price > 0
                else 0.0
            )

            if difference < 0:

                position = (
                    "COMPETITOR_CHEAPER"
                )

            elif difference > 0:

                position = (
                    "COMPETITOR_MORE_EXPENSIVE"
                )

            else:

                position = "EQUAL"

            client_price_info = {
                "client_price": (
                    f"{client_price:.2f}"
                ),

                "client_currency": (
                    offering.currency
                ),

                "competitor_price": (
                    f"{competitor_price:.2f}"
                ),

                "competitor_currency": (
                    collection_result.currency
                ),

                "price_difference": (
                    f"{difference:+.2f}"
                ),

                "percentage_difference": (
                    f"{percentage_difference:+.2f}%"
                ),

                "position": position,
            }

        # =====================================================
        # 9. UPDATE JOB
        # =====================================================

        if collection_result.success:

            job.status = (
                JobStatusEnum.COMPLETED
            )

            job.total_items_processed = 1
            job.successful_items = 1
            job.failed_items = 0

            job.error_category = None
            job.error_message = None

        else:

            job.status = (
                JobStatusEnum.FAILED
            )

            job.total_items_processed = 1
            job.successful_items = 0
            job.failed_items = 1

            job.error_message = (
                collection_result.error
                or "Collection failed."
            )

        # =====================================================
        # 10. COMMIT
        # =====================================================

        await session.commit()

        await session.refresh(
            job
        )

        # =====================================================
        # 11. FAILED COLLECTION
        # =====================================================

        if not collection_result.success:

            raise HTTPException(
                status_code=(
                    status.HTTP_502_BAD_GATEWAY
                ),

                detail={
                    "message": (
                        "Collection failed."
                    ),

                    "error": (
                        collection_result.error
                    ),

                    "extraction_status": (
                        collection_result.extraction_status
                    ),

                    "job_id": str(
                        job.id
                    ),
                },
            )

        # =====================================================
        # 12. CONFIDENCE
        # =====================================================

        if (
            collection_result.price is not None
            and collection_result.currency is not None
            and collection_result.availability is not None
        ):

            confidence = "HIGH"

        elif (
            collection_result.price is not None
            or collection_result.currency is not None
            or collection_result.availability is not None
        ):

            confidence = "MEDIUM"

        else:

            confidence = "LOW"

        # =====================================================
        # 13. FINAL RESPONSE
        # =====================================================

        return {
            "job_id": str(
                job.id
            ),

            "offering_match_id": str(
                offering_match_id_val
            ),

            "target_url": target_url_val,

            "source_id": (
                str(job.source_id)
                if job.source_id
                else None
            ),

            "source_name": (
                source.name
                if source
                else None
            ),

            "competitor_id": (
                str(competitor.id)
                if competitor
                else None
            ),

            "competitor_name": (
                competitor.name
                if competitor
                else None
            ),

            "offering_id": (
                str(offering.id)
                if offering
                else None
            ),

            "offering_name": (
                offering.name
                if offering
                else None
            ),

            "status": job.status,

            "message": (
                "Collection completed successfully."
            ),

            # -------------------------------------------------
            # Collection diagnostics
            # -------------------------------------------------

            "collection": {
                "success": (
                    collection_result.success
                ),

                "extraction_status": (
                    collection_result.extraction_status
                ),

                "response_time_ms": (
                    collection_result.response_time_ms
                ),

                "http_status_code": (
                    collection_result.status_code
                ),

                "content_type": (
                    collection_result.content_type
                ),

                "content_length": (
                    collection_result.content_length
                ),

                "error": (
                    collection_result.error
                ),
            },

            # -------------------------------------------------
            # Observation
            # -------------------------------------------------

            "observation": {
                "price": (
                    f"{float(collection_result.price):.2f}"
                    if collection_result.price
                    is not None
                    else None
                ),

                "currency": (
                    collection_result.currency
                ),

                "availability": (
                    availability.value
                ),

                "response_time_ms": (
                    collection_result.response_time_ms
                ),

                "http_status_code": (
                    collection_result.status_code
                ),

                "confidence": confidence,

                "extraction_status": (
                    collection_result.extraction_status
                ),

                "extracted_attributes": (
                    collection_result.attributes
                    or {}
                ),

                "observed_at": (
                    observation.observed_at.isoformat()
                    if observation.observed_at
                    else None
                ),
            },

            # -------------------------------------------------
            # Snapshot
            # -------------------------------------------------

            "snapshot": {
                "current_price": (
                    f"{float(snapshot.current_price):.2f}"
                    if snapshot.current_price
                    is not None
                    else None
                ),

                "previous_price": (
                    f"{float(snapshot.previous_price):.2f}"
                    if snapshot.previous_price
                    is not None
                    else None
                ),

                "price_difference": (
                    f"{float(snapshot.price_difference):+.2f}"
                    if snapshot.price_difference
                    is not None
                    else None
                ),

                "percentage_difference": (
                    f"{snapshot.percentage_difference:+.2f}%"
                    if snapshot.percentage_difference
                    is not None
                    else None
                ),

                "price_movement": (
                    price_movement
                ),

                "current_availability": (
                    snapshot.current_availability.value
                    if snapshot.current_availability
                    else None
                ),

                "last_observed_at": (
                    snapshot.last_observed_at.isoformat()
                    if snapshot.last_observed_at
                    else None
                ),
            },

            # -------------------------------------------------
            # Competitive comparison
            # -------------------------------------------------

            "competitive_comparison": (
                client_price_info
            ),
        }

    # =========================================================
    # UNEXPECTED APPLICATION / DB ERROR
    # =========================================================

    except HTTPException:
        raise

    except Exception as exc:

        await session.rollback()

        failed_job = JobModel(
            source_id=source_id_val,

            job_type=(
                JobTypeEnum.ON_DEMAND_REFRESH
            ),

            status=(
                JobStatusEnum.FAILED
            ),

            total_items_processed=1,

            successful_items=0,

            failed_items=1,

            error_message=str(exc),

            meta_info={
                "offering_match_id": str(
                    offering_match_id_val
                ),

                "target_url": target_url_val,
            },
        )

        session.add(
            failed_job
        )

        await session.commit()

        await session.refresh(
            failed_job
        )

        raise HTTPException(
            status_code=(
                status.HTTP_502_BAD_GATEWAY
            ),

            detail={
                "message": (
                    "Collection failed."
                ),

                "error": str(exc),

                "job_id": str(
                    failed_job.id
                ),
            },
        )
