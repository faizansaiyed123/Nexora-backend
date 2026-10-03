"""
Offering Service Layer.
Implements the business logic for the Offering & Dynamic Catalog Management system:
- Tenant-scoped CRUD and filtering
- Plan limit enforcement (max_tracked_offerings)
- SSRF prevention and URL validation
- JSONB attribute sanitization
- Automatic dynamic field discovery and schema registration
- Competitor intelligence enrichment aggregation (OfferingMatchModel, SourceModel, CompetitorModel, SnapshotModel)
- Bulk ingestion up to 1,000 items with index-attributed error tracking
- Soft-archive and bulk soft-archive
- Monitoring toggle
- CSV and JSON catalog export
- Dynamic field definitions CRUD
"""

import csv
from datetime import datetime, timezone
from decimal import Decimal
import io
import math
from typing import Any, Dict, List, Optional, Tuple
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.models.client import ClientModel
from backend.models.competitor import CompetitorModel, OfferingMatchModel, SourceModel
from backend.models.enums import CreatedViaEnum, MatchStatusEnum, OfferingTypeEnum
from backend.models.observation import ObservationModel, SnapshotModel
from backend.models.offering import DynamicFieldDefinitionModel, OfferingModel
from backend.schemas.offering import (
    BulkImportErrorItem,
    BulkOfferingArchiveRequest,
    BulkOfferingArchiveResponse,
    BulkOfferingImportRequest,
    BulkOfferingImportResponse,
    CompetitorMatchSummary,
    DynamicFieldDefinitionCreate,
    OfferingCreate,
    OfferingDetailRead,
    OfferingPaginationResponse,
    OfferingRead,
    OfferingUpdate,
    ToggleMonitoringResponse,
)
from backend.schemas.observation import ObservationHistoryItem, ObservationHistoryResponse
from backend.services.url_security import SecurityValidationError, UrlSecurityService


class OfferingService:
    """Service handling offering operations, catalog management, and competitor enrichment."""

    # --------------------------------------------------------------------------
    # Helper: Plan Limit Check
    # --------------------------------------------------------------------------
    @staticmethod
    async def _check_catalog_limit(
        db: AsyncSession,
        client_id: uuid.UUID,
        adding_count: int = 1,
    ) -> None:
        """
        Verifies that the tenant has not exceeded their active offerings quota.
        """
        # Fetch client's max_tracked_offerings
        client_res = await db.execute(
            select(ClientModel.max_tracked_offerings).where(ClientModel.id == client_id).with_for_update()
        )
        max_allowed = client_res.scalar_one_or_none()
        if max_allowed is None:
            max_allowed = 500  # Default fallback

        # Count current non-archived offerings
        count_res = await db.execute(
            select(func.count(OfferingModel.id)).where(
                OfferingModel.client_id == client_id,
                OfferingModel.is_archived.is_(False),
            )
        )
        current_active = count_res.scalar_one() or 0

        if current_active + adding_count > max_allowed:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Plan limit reached. Your active offerings count ({current_active}) + "
                    f"new ({adding_count}) exceeds your plan limit of {max_allowed}. "
                    "Please upgrade your subscription or archive unused offerings."
                ),
            )

    # --------------------------------------------------------------------------
    # Dynamic Field Discovery Helper
    # --------------------------------------------------------------------------
    @staticmethod
    async def _auto_discover_dynamic_fields(
        db: AsyncSession,
        client_id: uuid.UUID,
        attributes: Dict[str, Any],
    ) -> None:
        """
        Auto-registers newly encountered dynamic attribute keys in dynamic_field_definitions.
        Infers data type: NUMBER, BOOLEAN, DECIMAL, DATE, or STRING.
        """
        if not attributes or not isinstance(attributes, dict):
            return

        # Fetch existing defined field names for this client
        existing_res = await db.execute(
            select(DynamicFieldDefinitionModel.field_name).where(
                DynamicFieldDefinitionModel.client_id == client_id
            )
        )
        existing_fields = set(existing_res.scalars().all())

        new_definitions: List[DynamicFieldDefinitionModel] = []

        for key, val in attributes.items():
            if key in existing_fields:
                continue

            # Infer type
            inferred_type = "STRING"
            if isinstance(val, bool):
                inferred_type = "BOOLEAN"
            elif isinstance(val, int):
                inferred_type = "NUMBER"
            elif isinstance(val, (float, Decimal)):
                inferred_type = "DECIMAL"
            elif isinstance(val, str):
                # Check for ISO date string
                if len(val) >= 10 and (val[4] == "-" and val[7] == "-"):
                    try:
                        datetime.fromisoformat(val.replace("Z", "+00:00"))
                        inferred_type = "DATE"
                    except ValueError:
                        inferred_type = "STRING"
                else:
                    inferred_type = "STRING"

            # Create human-friendly display name
            display_name = key.replace("_", " ").replace("-", " ").title()

            new_field = DynamicFieldDefinitionModel(
                client_id=client_id,
                field_name=key,
                display_name=display_name,
                data_type=inferred_type,
                unit=None,
                category="GENERAL",
                description=f"Auto-discovered attribute '{key}'",
                is_selected=True,
                confidence="HIGH",
                is_required=False,
                is_comparable=True,
                options=None,
            )
            new_definitions.append(new_field)
            existing_fields.add(key)

        if new_definitions:
            db.add_all(new_definitions)
            await db.flush()

    # --------------------------------------------------------------------------
    # Competitor Enrichment Calculation Helper
    # --------------------------------------------------------------------------
    @staticmethod
    async def _compute_competitor_metrics(
        db: AsyncSession,
        offering_ids: List[uuid.UUID],
        client_id: uuid.UUID,
    ) -> Dict[uuid.UUID, Dict[str, Any]]:
        """
        Calculates aggregate competitor match intelligence for a batch of offerings:
        - competitor_matches_count
        - min_competitor_price
        - avg_competitor_price
        - price_delta_percent
        """
        if not offering_ids:
            return {}

        metrics: Dict[uuid.UUID, Dict[str, Any]] = {
            oid: {
                "competitor_matches_count": 0,
                "min_competitor_price": None,
                "avg_competitor_price": None,
            }
            for oid in offering_ids
        }

        # Query active matches with competitor and snapshot
        stmt = (
            select(
                OfferingMatchModel.offering_id,
                func.count(OfferingMatchModel.id).label("matches_count"),
                func.min(SnapshotModel.current_price).label("min_price"),
                func.avg(SnapshotModel.current_price).label("avg_price"),
            )
            .join(SourceModel, OfferingMatchModel.source_id == SourceModel.id)
            .join(CompetitorModel, SourceModel.competitor_id == CompetitorModel.id)
            .outerjoin(SnapshotModel, SnapshotModel.offering_match_id == OfferingMatchModel.id)
            .where(
                OfferingMatchModel.offering_id.in_(offering_ids),
                CompetitorModel.client_id == client_id,
                OfferingMatchModel.is_active.is_(True),
                OfferingMatchModel.match_status != MatchStatusEnum.REJECTED,
            )
            .group_by(OfferingMatchModel.offering_id)
        )

        res = await db.execute(stmt)
        for row in res.all():
            oid = row.offering_id
            metrics[oid] = {
                "competitor_matches_count": row.matches_count or 0,
                "min_competitor_price": Decimal(str(row.min_price)) if row.min_price is not None else None,
                "avg_competitor_price": Decimal(str(row.avg_price)).quantize(Decimal("0.0001")) if row.avg_price is not None else None,
            }

        return metrics

    # --------------------------------------------------------------------------
    # Single Offering: Create
    # --------------------------------------------------------------------------
    @staticmethod
    async def create(
        db: AsyncSession,
        client_id: uuid.UUID,
        data: OfferingCreate,
    ) -> OfferingModel:
        """
        Creates a single offering with SSRF validation, JSONB sanitization,
        quota checking, and automatic dynamic field discovery.
        """
        # 1. Enforce plan limit
        await OfferingService._check_catalog_limit(db, client_id, adding_count=1)

        # 2. Validate URLs
        validated_url = None
        if data.url:
            try:
                validated_url = UrlSecurityService.validate_url(data.url)
            except SecurityValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"URL security validation failed: {str(e)}",
                )

        validated_image_url = None
        if data.image_url:
            try:
                validated_image_url = UrlSecurityService.validate_url(data.image_url)
            except SecurityValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Image URL security validation failed: {str(e)}",
                )

        # 3. Sanitize JSONB dynamic attributes
        try:
            sanitized_attributes = UrlSecurityService.sanitize_attributes(data.attributes)
        except SecurityValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Attributes security validation failed: {str(e)}",
            )

        # 4. Auto-discover schema fields
        await OfferingService._auto_discover_dynamic_fields(db, client_id, sanitized_attributes)

        # 5. Construct Offering model
        offering = OfferingModel(
            client_id=client_id,
            name=data.name,
            offering_type=data.offering_type,
            custom_type_name=data.custom_type_name,
            sku=data.sku,
            current_price=data.current_price,
            currency=data.currency,
            market=data.market,
            url=validated_url,
            category=data.category,
            image_url=validated_image_url,
            description=data.description,
            attributes=sanitized_attributes,
            is_archived=False,
            is_monitored=data.is_monitored,
            created_via=data.created_via,
        )

        db.add(offering)
        await db.commit()
        await db.refresh(offering)

        return offering

    # --------------------------------------------------------------------------
    # Single Offering: Get by ID with enriched details & matches
    # --------------------------------------------------------------------------
    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
    ) -> Optional[OfferingDetailRead]:
        """
        Retrieves a single offering by ID with complete competitor match details,
        snapshots, and aggregated price difference metrics.
        """
        stmt = (
            select(OfferingModel)
            .options(
                selectinload(OfferingModel.matches)
                .selectinload(OfferingMatchModel.source)
                .selectinload(SourceModel.competitor),
                selectinload(OfferingModel.matches)
                .selectinload(OfferingMatchModel.snapshot),
            )
            .where(
                OfferingModel.id == offering_id,
                OfferingModel.client_id == client_id,
            )
        )
        res = await db.execute(stmt)
        offering = res.scalar_one_or_none()
        if not offering:
            return None

        # Build matches summary list
        match_summaries: List[CompetitorMatchSummary] = []
        comp_prices: List[Decimal] = []

        for m in offering.matches:
            if not m.is_active or m.match_status == MatchStatusEnum.REJECTED:
                continue

            current_comp_price = None
            availability = None
            last_observed_at = None
            price_delta = None

            if m.snapshot:
                if m.snapshot.current_price is not None:
                    current_comp_price = Decimal(str(m.snapshot.current_price))
                    comp_prices.append(current_comp_price)
                    if offering.current_price and offering.current_price > 0:
                        delta = ((current_comp_price - offering.current_price) / offering.current_price) * Decimal("100")
                        price_delta = delta.quantize(Decimal("0.01"))

                availability = m.snapshot.current_availability
                last_observed_at = m.snapshot.last_observed_at

            competitor_name = m.source.competitor.name if m.source and m.source.competitor else "Unknown"
            source_name = m.source.name if m.source else "Unknown"

            match_summaries.append(
                CompetitorMatchSummary(
                    match_id=m.id,
                    competitor_id=m.source.competitor_id if m.source else uuid.uuid4(),
                    competitor_name=competitor_name,
                    source_id=m.source_id,
                    source_name=source_name,
                    target_url=m.target_url,
                    match_status=m.match_status,
                    confidence_score=m.confidence_score,
                    current_price=current_comp_price,
                    currency=offering.currency,
                    availability=availability,
                    last_observed_at=last_observed_at,
                    price_delta_percent=price_delta,
                )
            )

        # Calculate summary metrics
        matches_count = len(match_summaries)
        min_comp_price = min(comp_prices) if comp_prices else None
        avg_comp_price = (sum(comp_prices) / Decimal(len(comp_prices))).quantize(Decimal("0.0001")) if comp_prices else None
        price_delta_pct = None
        if min_comp_price is not None and offering.current_price and offering.current_price > 0:
            delta = ((min_comp_price - offering.current_price) / offering.current_price) * Decimal("100")
            price_delta_pct = delta.quantize(Decimal("0.01"))

        detail = OfferingDetailRead(
            id=offering.id,
            client_id=offering.client_id,
            name=offering.name,
            offering_type=offering.offering_type,
            custom_type_name=offering.custom_type_name,
            sku=offering.sku,
            current_price=offering.current_price,
            currency=offering.currency,
            market=offering.market,
            url=offering.url,
            category=offering.category,
            image_url=offering.image_url,
            description=offering.description,
            attributes=offering.attributes or {},
            is_archived=offering.is_archived,
            is_monitored=offering.is_monitored,
            created_via=offering.created_via,
            competitor_matches_count=matches_count,
            min_competitor_price=min_comp_price,
            avg_competitor_price=avg_comp_price,
            price_delta_percent=price_delta_pct,
            created_at=offering.created_at,
            updated_at=offering.updated_at,
            matches=match_summaries,
        )

        return detail

    @staticmethod
    async def observation_history(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
        match_id: Optional[uuid.UUID] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ObservationHistoryResponse:
        page = max(1, page)
        page_size = min(100, max(1, page_size))

        filters = [
            OfferingModel.id == offering_id,
            OfferingModel.client_id == client_id,
        ]
        if match_id is not None:
            filters.append(OfferingMatchModel.id == match_id)

        total = await db.scalar(
            select(func.count(ObservationModel.id))
            .select_from(ObservationModel)
            .join(OfferingMatchModel, ObservationModel.offering_match_id == OfferingMatchModel.id)
            .join(OfferingModel, OfferingMatchModel.offering_id == OfferingModel.id)
            .join(SourceModel, OfferingMatchModel.source_id == SourceModel.id)
            .join(CompetitorModel, SourceModel.competitor_id == CompetitorModel.id)
            .where(*filters)
        ) or 0

        result = await db.execute(
            select(ObservationModel, OfferingMatchModel, SourceModel, CompetitorModel)
            .join(OfferingMatchModel, ObservationModel.offering_match_id == OfferingMatchModel.id)
            .join(OfferingModel, OfferingMatchModel.offering_id == OfferingModel.id)
            .join(SourceModel, OfferingMatchModel.source_id == SourceModel.id)
            .join(CompetitorModel, SourceModel.competitor_id == CompetitorModel.id)
            .where(*filters)
            .order_by(ObservationModel.observed_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        items = [
            ObservationHistoryItem(
                id=observation.id,
                offering_match_id=observation.offering_match_id,
                competitor_id=competitor.id,
                competitor_name=competitor.name,
                source_id=source.id,
                source_name=source.name,
                target_url=match.target_url,
                observed_at=observation.observed_at,
                observed_price=observation.observed_price,
                currency=observation.currency,
                availability=observation.availability,
                response_time_ms=observation.response_time_ms,
                http_status_code=observation.http_status_code,
            )
            for observation, match, source, competitor in result.all()
        ]

        total_pages = math.ceil(total / page_size) if total else 0
        return ObservationHistoryResponse(
            items=items,
            total_count=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_more=page < total_pages,
        )

    # --------------------------------------------------------------------------
    # List & Search Offerings with Filtering & Pagination
    # --------------------------------------------------------------------------
    @staticmethod
    async def list_offerings(
        db: AsyncSession,
        client_id: uuid.UUID,
        q: Optional[str] = None,
        offering_type: Optional[OfferingTypeEnum] = None,
        category: Optional[str] = None,
        market: Optional[str] = None,
        currency: Optional[str] = None,
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        is_monitored: Optional[bool] = None,
        include_archived: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> OfferingPaginationResponse:
        """
        Lists offerings with multi-field search, facet filtering, pagination,
        and batch competitor enrichment.
        """
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50
        if page_size > 500:
            page_size = 500

        # Base filter condition
        filters = [OfferingModel.client_id == client_id]

        if not include_archived:
            filters.append(OfferingModel.is_archived.is_(False))

        if q:
            search_pattern = f"%{q.strip()}%"
            filters.append(
                or_(
                    OfferingModel.name.ilike(search_pattern),
                    OfferingModel.sku.ilike(search_pattern),
                    OfferingModel.category.ilike(search_pattern),
                    OfferingModel.description.ilike(search_pattern),
                )
            )

        if offering_type:
            filters.append(OfferingModel.offering_type == offering_type)

        if category:
            filters.append(OfferingModel.category.ilike(f"%{category.strip()}%"))

        if market:
            filters.append(OfferingModel.market == market.strip().upper())

        if currency:
            filters.append(OfferingModel.currency == currency.strip().upper())

        if min_price is not None:
            filters.append(OfferingModel.current_price >= min_price)

        if max_price is not None:
            filters.append(OfferingModel.current_price <= max_price)

        if is_monitored is not None:
            filters.append(OfferingModel.is_monitored.is_(is_monitored))

        # Total count query
        count_stmt = select(func.count(OfferingModel.id)).where(*filters)
        total_res = await db.execute(count_stmt)
        total_count = total_res.scalar_one() or 0

        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0
        has_more = page < total_pages

        # Query page records
        offset = (page - 1) * page_size
        items_stmt = (
            select(OfferingModel)
            .where(*filters)
            .order_by(OfferingModel.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items_res = await db.execute(items_stmt)
        offerings = list(items_res.scalars().all())

        # Enrich with competitor metrics
        offering_ids = [o.id for o in offerings]
        metrics_map = await OfferingService._compute_competitor_metrics(db, offering_ids, client_id)

        items_read: List[OfferingRead] = []
        for o in offerings:
            m = metrics_map.get(o.id, {})
            min_p = m.get("min_competitor_price")
            avg_p = m.get("avg_competitor_price")
            delta_pct = None
            if min_p is not None and o.current_price and o.current_price > 0:
                delta = ((min_p - o.current_price) / o.current_price) * Decimal("100")
                delta_pct = delta.quantize(Decimal("0.01"))

            items_read.append(
                OfferingRead(
                    id=o.id,
                    client_id=o.client_id,
                    name=o.name,
                    offering_type=o.offering_type,
                    custom_type_name=o.custom_type_name,
                    sku=o.sku,
                    current_price=o.current_price,
                    currency=o.currency,
                    market=o.market,
                    url=o.url,
                    category=o.category,
                    image_url=o.image_url,
                    description=o.description,
                    attributes=o.attributes or {},
                    is_archived=o.is_archived,
                    is_monitored=o.is_monitored,
                    created_via=o.created_via,
                    competitor_matches_count=m.get("competitor_matches_count", 0),
                    min_competitor_price=min_p,
                    avg_competitor_price=avg_p,
                    price_delta_percent=delta_pct,
                    created_at=o.created_at,
                    updated_at=o.updated_at,
                )
            )

        return OfferingPaginationResponse(
            items=items_read,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_more=has_more,
        )

    # --------------------------------------------------------------------------
    # Single Offering: Update (Partial)
    # --------------------------------------------------------------------------
    @staticmethod
    async def update(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
        data: OfferingUpdate,
    ) -> Optional[OfferingModel]:
        """
        Updates an offering partially.
        Merges dynamic attributes dictionary rather than overwriting completely if both exist.
        """
        # Fetch offering
        res = await db.execute(
            select(OfferingModel).where(
                OfferingModel.id == offering_id,
                OfferingModel.client_id == client_id,
            )
        )
        offering = res.scalar_one_or_none()
        if not offering:
            return None

        update_fields = data.model_dump(exclude_unset=True)

        # Validate URL if updated
        if "url" in update_fields and update_fields["url"] is not None:
            try:
                update_fields["url"] = UrlSecurityService.validate_url(update_fields["url"])
            except SecurityValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"URL security validation failed: {str(e)}",
                )

        if "image_url" in update_fields and update_fields["image_url"] is not None:
            try:
                update_fields["image_url"] = UrlSecurityService.validate_url(update_fields["image_url"])
            except SecurityValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Image URL security validation failed: {str(e)}",
                )

        # Merge dynamic attributes if provided
        if "attributes" in update_fields and update_fields["attributes"] is not None:
            try:
                sanitized_new = UrlSecurityService.sanitize_attributes(update_fields["attributes"])
                merged_attrs = dict(offering.attributes or {})
                merged_attrs.update(sanitized_new)
                update_fields["attributes"] = merged_attrs
                # Discover any newly introduced fields
                await OfferingService._auto_discover_dynamic_fields(db, client_id, sanitized_new)
            except SecurityValidationError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Attributes security validation failed: {str(e)}",
                )

        for key, val in update_fields.items():
            setattr(offering, key, val)

        await db.commit()
        await db.refresh(offering)
        return offering

    # --------------------------------------------------------------------------
    # Single Offering: Soft Archive (DELETE /{id})
    # --------------------------------------------------------------------------
    @staticmethod
    async def soft_archive(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
    ) -> bool:
        """
        Soft-archives an offering (is_archived = True), preserving all historical
        matches, observations, snapshots, and price alerts.
        """
        res = await db.execute(
            select(OfferingModel).where(
                OfferingModel.id == offering_id,
                OfferingModel.client_id == client_id,
            )
        )
        offering = res.scalar_one_or_none()
        if not offering:
            return False

        offering.is_archived = True
        offering.is_monitored = False
        await db.commit()
        return True

    # --------------------------------------------------------------------------
    # Monitoring Toggle: POST /{id}/toggle-monitoring
    # --------------------------------------------------------------------------
    @staticmethod
    async def toggle_monitoring(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
    ) -> ToggleMonitoringResponse:
        """
        Toggles the is_monitored flag for an offering.
        """
        res = await db.execute(
            select(OfferingModel).where(
                OfferingModel.id == offering_id,
                OfferingModel.client_id == client_id,
            )
        )
        offering = res.scalar_one_or_none()
        if not offering:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Offering not found.",
            )

        if offering.is_archived:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot toggle monitoring on an archived offering. Please restore it first.",
            )

        offering.is_monitored = not offering.is_monitored
        await db.commit()
        await db.refresh(offering)

        status_str = "enabled" if offering.is_monitored else "disabled"
        return ToggleMonitoringResponse(
            id=offering.id,
            name=offering.name,
            is_monitored=offering.is_monitored,
            message=f"Monitoring {status_str} for offering '{offering.name}'.",
        )

    # --------------------------------------------------------------------------
    # Bulk Ingestion: Up to 1,000 items with index-attributed error tracking
    # --------------------------------------------------------------------------
    @staticmethod
    async def bulk_create(
        db: AsyncSession,
        client_id: uuid.UUID,
        request: BulkOfferingImportRequest,
    ) -> BulkOfferingImportResponse:
        """
        Processes up to 1,000 offerings in a single call.
        Validates URLs, sanitizes JSONB attributes, discovers dynamic schemas,
        and provides index-attributed error feedback for any failed rows.
        """
        items = request.items
        total_items = len(items)

        # Plan limit check for total batch
        # Count current active offerings
        client_res = await db.execute(
            select(ClientModel.max_tracked_offerings).where(ClientModel.id == client_id).with_for_update()
        )
        max_allowed = client_res.scalar_one_or_none() or 500

        count_res = await db.execute(
            select(func.count(OfferingModel.id)).where(
                OfferingModel.client_id == client_id,
                OfferingModel.is_archived.is_(False),
            )
        )
        current_active = count_res.scalar_one() or 0
        remaining_slots = max(0, max_allowed - current_active)

        successful_offerings: List[OfferingModel] = []
        errors: List[BulkImportErrorItem] = []
        all_attributes_to_discover: Dict[str, Any] = {}

        for idx, item in enumerate(items):
            # Check slot capacity
            if len(successful_offerings) >= remaining_slots:
                errors.append(
                    BulkImportErrorItem(
                        index=idx,
                        sku=item.sku,
                        name=item.name,
                        error=f"Plan capacity exceeded. Max active offerings limit is {max_allowed}.",
                    )
                )
                continue

            # Validate URL
            validated_url = None
            if item.url:
                try:
                    validated_url = UrlSecurityService.validate_url(item.url)
                except SecurityValidationError as e:
                    errors.append(
                        BulkImportErrorItem(
                            index=idx,
                            sku=item.sku,
                            name=item.name,
                            error=f"URL validation failed: {str(e)}",
                        )
                    )
                    continue

            # Validate Image URL
            validated_image_url = None
            if item.image_url:
                try:
                    validated_image_url = UrlSecurityService.validate_url(item.image_url)
                except SecurityValidationError as e:
                    errors.append(
                        BulkImportErrorItem(
                            index=idx,
                            sku=item.sku,
                            name=item.name,
                            error=f"Image URL validation failed: {str(e)}",
                        )
                    )
                    continue

            # Sanitize Dynamic Attributes
            try:
                sanitized_attrs = UrlSecurityService.sanitize_attributes(item.attributes)
            except SecurityValidationError as e:
                errors.append(
                    BulkImportErrorItem(
                        index=idx,
                        sku=item.sku,
                        name=item.name,
                        error=f"Attributes validation failed: {str(e)}",
                    )
                )
                continue

            all_attributes_to_discover.update(sanitized_attrs)

            offering = OfferingModel(
                client_id=client_id,
                name=item.name,
                offering_type=item.offering_type,
                custom_type_name=item.custom_type_name,
                sku=item.sku,
                current_price=item.current_price,
                currency=item.currency,
                market=item.market,
                url=validated_url,
                category=item.category,
                image_url=validated_image_url,
                description=item.description,
                attributes=sanitized_attrs,
                is_archived=False,
                is_monitored=item.is_monitored,
                created_via=item.created_via,
            )
            successful_offerings.append(offering)

        # Auto discover any newly encountered schema fields
        if all_attributes_to_discover:
            await OfferingService._auto_discover_dynamic_fields(db, client_id, all_attributes_to_discover)

        if successful_offerings:
            db.add_all(successful_offerings)
            await db.commit()

        return BulkOfferingImportResponse(
            total_processed=total_items,
            successful_count=len(successful_offerings),
            failed_count=len(errors),
            errors=errors,
        )

    # --------------------------------------------------------------------------
    # Bulk Archive: POST /bulk-archive
    # --------------------------------------------------------------------------
    @staticmethod
    async def bulk_archive(
        db: AsyncSession,
        client_id: uuid.UUID,
        request: BulkOfferingArchiveRequest,
    ) -> BulkOfferingArchiveResponse:
        """
        Soft-archives up to 1,000 offerings matching tenant client_id.
        """
        target_ids = list(set(request.offering_ids))
        if not target_ids:
            return BulkOfferingArchiveResponse(
                total_requested=0,
                archived_count=0,
                archived_ids=[],
            )

        stmt = (
            update(OfferingModel)
            .where(
                OfferingModel.id.in_(target_ids),
                OfferingModel.client_id == client_id,
                OfferingModel.is_archived.is_(False),
            )
            .values(
                is_archived=True,
                is_monitored=False,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(OfferingModel.id)
        )

        res = await db.execute(stmt)
        archived_ids = list(res.scalars().all())
        await db.commit()

        return BulkOfferingArchiveResponse(
            total_requested=len(target_ids),
            archived_count=len(archived_ids),
            archived_ids=archived_ids,
        )

    # --------------------------------------------------------------------------
    # Dynamic Field Definitions: CRUD
    # --------------------------------------------------------------------------
    @staticmethod
    async def list_dynamic_fields(
        db: AsyncSession,
        client_id: uuid.UUID,
    ) -> List[DynamicFieldDefinitionModel]:
        """Lists all dynamic field definitions configured or discovered for a tenant."""
        res = await db.execute(
            select(DynamicFieldDefinitionModel)
            .where(DynamicFieldDefinitionModel.client_id == client_id)
            .order_by(DynamicFieldDefinitionModel.field_name.asc())
        )
        return list(res.scalars().all())

    @staticmethod
    async def create_dynamic_field(
        db: AsyncSession,
        client_id: uuid.UUID,
        data: DynamicFieldDefinitionCreate,
    ) -> DynamicFieldDefinitionModel:
        """Creates or updates a client dynamic field definition."""
        # Check if already exists
        res = await db.execute(
            select(DynamicFieldDefinitionModel).where(
                DynamicFieldDefinitionModel.client_id == client_id,
                DynamicFieldDefinitionModel.field_name == data.field_name,
            )
        )
        existing = res.scalar_one_or_none()

        if existing:
            existing.display_name = data.display_name
            existing.data_type = data.data_type
            existing.unit = data.unit
            existing.category = data.category
            existing.description = data.description
            existing.is_selected = data.is_selected
            existing.confidence = data.confidence
            existing.is_required = data.is_required
            existing.is_comparable = data.is_comparable
            existing.options = data.options
            await db.commit()
            await db.refresh(existing)
            return existing

        new_field = DynamicFieldDefinitionModel(
            client_id=client_id,
            field_name=data.field_name,
            display_name=data.display_name,
            data_type=data.data_type,
            unit=data.unit,
            category=data.category,
            description=data.description,
            is_selected=data.is_selected,
            confidence=data.confidence,
            is_required=data.is_required,
            is_comparable=data.is_comparable,
            options=data.options,
        )
        db.add(new_field)
        await db.commit()
        await db.refresh(new_field)
        return new_field

    @staticmethod
    async def delete_dynamic_field(
        db: AsyncSession,
        client_id: uuid.UUID,
        field_id: uuid.UUID,
    ) -> bool:
        """Deletes a dynamic field definition for a tenant."""
        res = await db.execute(
            select(DynamicFieldDefinitionModel).where(
                DynamicFieldDefinitionModel.id == field_id,
                DynamicFieldDefinitionModel.client_id == client_id,
            )
        )
        field_def = res.scalar_one_or_none()
        if not field_def:
            return False

        await db.delete(field_def)
        await db.commit()
        return True

    # --------------------------------------------------------------------------
    # Export Catalog: CSV & JSON
    # --------------------------------------------------------------------------
    @staticmethod
    async def export_catalog(
        db: AsyncSession,
        client_id: uuid.UUID,
        format_type: str = "csv",
        include_archived: bool = False,
    ) -> Tuple[str, str, str]:
        """
        Exports the entire tenant catalog in CSV (flattened dynamic attributes) or JSON format.
        Returns: (content_string, media_type, filename)
        """
        filters = [OfferingModel.client_id == client_id]
        if not include_archived:
            filters.append(OfferingModel.is_archived.is_(False))

        res = await db.execute(
            select(OfferingModel)
            .where(*filters)
            .order_by(OfferingModel.created_at.desc())
        )
        offerings = list(res.scalars().all())

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        if format_type.lower() == "json":
            import json
            catalog_list = []
            for o in offerings:
                catalog_list.append({
                    "id": str(o.id),
                    "name": o.name,
                    "offering_type": o.offering_type.value if hasattr(o.offering_type, "value") else str(o.offering_type),
                    "custom_type_name": o.custom_type_name,
                    "sku": o.sku,
                    "current_price": float(o.current_price) if o.current_price is not None else None,
                    "currency": o.currency,
                    "market": o.market,
                    "url": o.url,
                    "category": o.category,
                    "image_url": o.image_url,
                    "description": o.description,
                    "attributes": o.attributes or {},
                    "is_archived": o.is_archived,
                    "is_monitored": o.is_monitored,
                    "created_via": o.created_via.value if hasattr(o.created_via, "value") else str(o.created_via),
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                    "updated_at": o.updated_at.isoformat() if o.updated_at else None,
                })
            content = json.dumps(catalog_list, indent=2)
            return content, "application/json", f"catalog_export_{timestamp}.json"

        # CSV Format with Flattened Dynamic Attributes
        # 1. Collect all unique attribute keys across offerings
        dynamic_keys: set = set()
        for o in offerings:
            if o.attributes and isinstance(o.attributes, dict):
                dynamic_keys.update(o.attributes.keys())

        sorted_dynamic_keys = sorted(list(dynamic_keys))

        # 2. Build CSV columns
        base_headers = [
            "id",
            "name",
            "sku",
            "offering_type",
            "custom_type_name",
            "current_price",
            "currency",
            "market",
            "category",
            "url",
            "image_url",
            "description",
            "is_monitored",
            "is_archived",
            "created_via",
            "created_at",
        ]
        attribute_headers = [f"attr_{k}" for k in sorted_dynamic_keys]
        all_headers = base_headers + attribute_headers

        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(all_headers)

        for o in offerings:
            row = [
                str(o.id),
                o.name,
                o.sku or "",
                o.offering_type.value if hasattr(o.offering_type, "value") else str(o.offering_type),
                o.custom_type_name or "",
                str(o.current_price) if o.current_price is not None else "",
                o.currency,
                o.market,
                o.category or "",
                o.url or "",
                o.image_url or "",
                o.description or "",
                "true" if o.is_monitored else "false",
                "true" if o.is_archived else "false",
                o.created_via.value if hasattr(o.created_via, "value") else str(o.created_via),
                o.created_at.isoformat() if o.created_at else "",
            ]

            attrs = o.attributes or {}
            for k in sorted_dynamic_keys:
                val = attrs.get(k, "")
                if isinstance(val, (dict, list)):
                    import json
                    row.append(json.dumps(val))
                else:
                    row.append(str(val) if val is not None else "")

            writer.writerow(row)

        return output.getvalue(), "text/csv", f"catalog_export_{timestamp}.csv"
