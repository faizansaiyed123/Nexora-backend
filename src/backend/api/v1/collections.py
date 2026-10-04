import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.deps import AuthenticatedUserContext, get_current_user_claims, require_analyst
from backend.core.rate_limit import DistributedRateLimiter
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
from backend.services.alert_service import AlertService
from backend.collectors.browser import render_page
from backend.services.collection_service import CollectionService
from backend.core.error_messages import safe_collection_error
from backend.services.source_service import SourceService


router = APIRouter()
logger = logging.getLogger("nexora.collection")


@router.post(
    "/run",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_analyst)],
)
async def run_collection(
    offering_match_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
):
    """
    Collect the current state of an offering match.

    Flow:
    1. Validate offering match and load related models.
    2. Create a RUNNING job.
    3. Fetch the target URL.
    4. Create an observation.
    5. Create/update the snapshot.
    6. Calculate competitive comparison.
    7. Mark the job COMPLETED or FAILED.
    8. Return detailed competitive intelligence.
    """

    # ---------------------------------------------------------
    # 1. Find offering match with related models
    # ---------------------------------------------------------

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
            # source.configuration is read further down while the collection
            # runs. Without an explicit selectinload that lazy load fires from
            # async context and raises MissingGreenlet, which the catch-all at
            # the bottom of this handler converts into a 502 for every source
            # that has a configuration row.
            selectinload(
                OfferingMatchModel.source
            ).selectinload(
                SourceModel.configuration
            ),
        )
        .where(
            OfferingMatchModel.id == offering_match_id,
            OfferingMatchModel.is_active.is_(True),
        )
    )

    offering_match = result.scalar_one_or_none()

    if (
        offering_match is None
        or offering_match.offering is None
        or offering_match.offering.client_id != auth_ctx.client_id
        or offering_match.source is None
        or offering_match.source.competitor is None
        or offering_match.source.competitor.client_id != auth_ctx.client_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering match not found.",
        )

    offering_match_id_val = offering_match.id
    target_url_val = offering_match.target_url
    source_id_val = offering_match.source_id

    offering = offering_match.offering
    source = offering_match.source

    competitor = (
        source.competitor
        if source
        else None
    )

    if not SourceService.before_collection(source):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Collection source is temporarily unavailable while its circuit breaker is open.",
        )

    # ---------------------------------------------------------
    # 2. Create collection job
    # ---------------------------------------------------------

    job = JobModel(
        source_id=source_id_val,
        job_type=JobTypeEnum.ON_DEMAND_REFRESH,
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

    # ---------------------------------------------------------
    # 3. Execute collection
    # ---------------------------------------------------------

    try:
        collector = CollectionService()
        configuration = source.configuration if source else None
        timeout = float(configuration.timeout_seconds) if configuration else collector.DEFAULT_TIMEOUT
        headers = configuration.custom_headers if configuration else None
        selectors = configuration.extraction_selectors if configuration else None

        if configuration:
            await DistributedRateLimiter.check_rate_limit(
                "source",
                str(source.id),
                max_requests=configuration.rate_limit_rpm,
                window_seconds=60,
            )
            if configuration.request_delay_seconds > 0:
                await asyncio.sleep(configuration.request_delay_seconds)

        collection_result = await collector.collect(
            target_url_val,
            timeout=timeout,
            headers=headers if isinstance(headers, dict) else None,
            custom_selectors=selectors if isinstance(selectors, dict) else None,
            max_retries=configuration.max_retries if configuration else 0,
        )

        if configuration and configuration.requires_javascript:
            rendered_html = await render_page(
                target_url_val,
                timeout_seconds=configuration.timeout_seconds,
            )
            if rendered_html:
                collection_result = collector.from_rendered_html(
                    rendered_html,
                    target_url_val,
                    response_time_ms=collection_result.response_time_ms,
                    status_code=collection_result.status_code or 200,
                    custom_selectors=selectors if isinstance(selectors, dict) else None,
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
                collection_result.attributes or {}
            ),
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
            ).with_for_update()
        )
        snapshot = snapshot_result.scalar_one_or_none()

        previous_price = snapshot.current_price if snapshot is not None else None
        previous_availability = snapshot.current_availability if snapshot is not None else None
        price_movement = "FIRST_OBSERVATION"

        # -----------------------------------------------------
        # 7. Create/update snapshot only for successful data
        # -----------------------------------------------------

        if collection_result.success:
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
            else:
                current_price = collection_result.price
                snapshot.previous_price = snapshot.current_price
                if current_price is not None:
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

                    if difference > 0:
                        price_movement = "INCREASE"
                    elif difference < 0:
                        price_movement = "DECREASE"
                    else:
                        price_movement = "UNCHANGED"
                else:
                    snapshot.price_difference = None
                    snapshot.percentage_difference = None

                snapshot.current_availability = availability
                snapshot.last_observed_at = observation.observed_at

        # -----------------------------------------------------
        # 8. Competitive comparison
        #
        # Client price = offerings.base_price
        # Competitor price = collected price
        # -----------------------------------------------------

        client_price_info = None

        if (
            offering
            and offering.base_price is not None
            and collection_result.price is not None
        ):
            client_price = float(
                offering.base_price
            )

            competitor_price = float(
                collection_result.price
            )

            price_difference = (
                competitor_price - client_price
            )

            percentage_difference = (
                (price_difference / client_price) * 100
                if client_price > 0
                else 0.0
            )

            if price_difference < 0:
                position = "COMPETITOR_CHEAPER"

            elif price_difference > 0:
                position = "COMPETITOR_MORE_EXPENSIVE"

            else:
                position = "EQUAL"

            client_price_info = {
                "client_price": (
                    f"{client_price:.2f}"
                ),
                "client_currency": (
                    offering.currency
                ),
                "price_difference": (
                    f"{price_difference:+.2f}"
                ),
                "percentage_difference": (
                    f"{percentage_difference:+.2f}%"
                ),
                "position": position,
            }

        circuit_tripped = False
        if collection_result.success:
            SourceService.record_collection_success(source)
            await AlertService.evaluate_and_trigger(
                session,
                client_id=auth_ctx.client_id,
                offering_id=offering.id if offering else None,
                offering_match_id=offering_match_id_val,
                offering_name=offering.name if offering else "Offering",
                competitor_name=competitor.name if competitor else "Competitor",
                source_name=source.name if source else "Source",
                client_price=offering.base_price if offering else None,
                previous_price=previous_price,
                current_price=collection_result.price,
                previous_availability=previous_availability,
                current_availability=availability,
                percentage_difference=snapshot.percentage_difference,
                circuit_tripped=circuit_tripped,
            )

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
            circuit_tripped = SourceService.record_collection_failure(source)

            job.status = JobStatusEnum.FAILED
            job.total_items_processed = 1
            job.successful_items = 0
            job.failed_items = 1
            job.error_message = (
                collection_result.error
                or "Collection failed."
            )

        # -----------------------------------------------------
        # 10. Commit
        # -----------------------------------------------------

        await session.commit()
        await session.refresh(job)

        # -----------------------------------------------------
        # 11. Handle collection failure
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

        # -----------------------------------------------------
        # 12. Return detailed response
        # -----------------------------------------------------

        return {
            "job_id": str(job.id),

            "offering_match_id": str(
                offering_match_id_val
            ),

            "target_url": target_url_val,

            "source_id": str(job.source_id),

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

            "observation": {
                "price": (
                    f"{float(collection_result.price):.2f}"
                    if collection_result.price is not None
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

                "confidence": (
                    "HIGH"
                    if collection_result.price is not None
                    else "LOW"
                ),

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

            "snapshot": {
                "current_price": (
                    f"{float(snapshot.current_price):.2f}"
                    if snapshot.current_price is not None
                    else None
                ),

                "previous_price": (
                    f"{float(snapshot.previous_price):.2f}"
                    if snapshot.previous_price is not None
                    else None
                ),

                "price_difference": (
                    f"{float(snapshot.price_difference):+.2f}"
                    if snapshot.price_difference is not None
                    else None
                ),

                "percentage_difference": (
                    f"{snapshot.percentage_difference:+.2f}%"
                    if snapshot.percentage_difference is not None
                    else None
                ),

                "price_movement": price_movement,
            },

            "competitive_comparison": client_price_info,
        }

    # ---------------------------------------------------------
    # Unexpected application/database error
    # ---------------------------------------------------------

    except HTTPException:
        raise

    except Exception as exc:

        logger.exception("Unexpected collection failure for offering match %s", offering_match_id_val)
        await session.rollback()

        failed_job = JobModel(
            source_id=source_id_val,
            job_type=JobTypeEnum.ON_DEMAND_REFRESH,
            status=JobStatusEnum.FAILED,
            total_items_processed=1,
            successful_items=0,
            failed_items=1,
            error_message=safe_collection_error(exc),
            meta_info={
                "offering_match_id": str(
                    offering_match_id_val
                ),
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
                "error": safe_collection_error(exc),
                "job_id": str(failed_job.id),
            },
        )
