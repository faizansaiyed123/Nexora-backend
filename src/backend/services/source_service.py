"""
Business logic for Sources.

All source operations are tenant-scoped through the competitor's client_id.
"""

import uuid
from datetime import datetime, timezone
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.exceptions import ForbiddenException, ValidationException
from backend.models.enums import CircuitStateEnum, CollectionMethodEnum, HealthStatusEnum
from backend.models.competitor import (
    CompetitorModel,
    SourceConfigurationModel,
    SourceModel,
)
from backend.services.url_security import UrlSecurityService
from backend.schemas.competitor import (
    SourceConfigurationUpdate,
    SourceCreate,
    SourceUpdate,
)


class SourceService:
    """Service layer for source management."""

    @staticmethod
    async def create(
        db: AsyncSession,
        client_id: uuid.UUID,
        data: SourceCreate,
    ) -> SourceModel:
        """Create a source belonging to a competitor of the authenticated client."""

        # Verify competitor belongs to this client.
        result = await db.execute(
            select(CompetitorModel).where(
                CompetitorModel.id == data.competitor_id,
                CompetitorModel.client_id == client_id,
            )
        )

        competitor = result.scalar_one_or_none()

        if not competitor:
            raise ForbiddenException(
                "Competitor not found.",
                code="COMPETITOR_NOT_FOUND",
            )

        # Validate before persistence so collection can never target internal hosts.
        if data.collection_method not in {CollectionMethodEnum.HTTP_FAST, CollectionMethodEnum.PLAYWRIGHT_BROWSER}:
            raise ValidationException(
                "This collection method is not available in the current runtime.",
                code="UNSUPPORTED_COLLECTION_METHOD",
            )
        safe_base_url = UrlSecurityService.validate_url(data.base_url)

        # Create source.
        source = SourceModel(
            competitor_id=data.competitor_id,
            name=data.name,
            base_url=safe_base_url,
            source_type=data.source_type,
            collection_method=data.collection_method,
        )

        db.add(source)
        await db.flush()

        # Create configuration if supplied.
        if data.configuration:
            configuration = SourceConfigurationModel(
                source_id=source.id,
                **data.configuration.model_dump(),
            )
            db.add(configuration)

        await db.commit()

        # Re-query the source with configuration loaded.
        result = await db.execute(
            select(SourceModel)
            .options(
                selectinload(SourceModel.configuration)
            )
            .where(SourceModel.id == source.id)
        )

        return result.scalar_one()

    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        client_id: uuid.UUID,
        source_id: uuid.UUID,
    ) -> SourceModel | None:
        """Get a source while enforcing tenant isolation."""

        result = await db.execute(
            select(SourceModel)
            .options(
                selectinload(SourceModel.configuration)
            )
            .join(
                CompetitorModel,
                SourceModel.competitor_id == CompetitorModel.id,
            )
            .where(
                SourceModel.id == source_id,
                CompetitorModel.client_id == client_id,
            )
        )

        return result.scalar_one_or_none()

    @staticmethod
    async def list(
        db: AsyncSession,
        client_id: uuid.UUID,
        competitor_id: uuid.UUID | None = None,
    ) -> List[SourceModel]:
        """List sources belonging to the authenticated client."""

        query = (
            select(SourceModel)
            .options(
                selectinload(SourceModel.configuration)
            )
            .join(
                CompetitorModel,
                SourceModel.competitor_id == CompetitorModel.id,
            )
            .where(
                CompetitorModel.client_id == client_id
            )
            .order_by(
                SourceModel.created_at.desc()
            )
        )

        if competitor_id:
            query = query.where(
                SourceModel.competitor_id == competitor_id
            )

        result = await db.execute(query)

        return list(result.scalars().all())

    @staticmethod
    async def update(
        db: AsyncSession,
        client_id: uuid.UUID,
        source_id: uuid.UUID,
        data: SourceUpdate,
    ) -> SourceModel | None:
        """Update a source belonging to the authenticated client."""

        source = await SourceService.get_by_id(
            db=db,
            client_id=client_id,
            source_id=source_id,
        )

        if not source:
            return None

        update_data = data.model_dump(
            exclude_unset=True
        )

        if "verification_status" in update_data:
            update_data["verification_status"] = update_data["verification_status"]


        if "collection_method" in update_data and update_data["collection_method"] not in {CollectionMethodEnum.HTTP_FAST, CollectionMethodEnum.PLAYWRIGHT_BROWSER}:
            raise ValidationException(
                "This collection method is not available in the current runtime.",
                code="UNSUPPORTED_COLLECTION_METHOD",
            )

        if "base_url" in update_data:
            update_data["base_url"] = UrlSecurityService.validate_url(update_data["base_url"])

        for field, value in update_data.items():
            setattr(source, field, value)

        await db.commit()

        # Re-query with configuration loaded.
        result = await db.execute(
            select(SourceModel)
            .options(
                selectinload(SourceModel.configuration)
            )
            .where(SourceModel.id == source.id)
        )

        return result.scalar_one()

    @staticmethod
    async def delete(
        db: AsyncSession,
        client_id: uuid.UUID,
        source_id: uuid.UUID,
    ) -> bool:
        """Delete a source belonging to the authenticated client."""

        source = await SourceService.get_by_id(
            db=db,
            client_id=client_id,
            source_id=source_id,
        )

        if not source:
            return False

        await db.delete(source)
        await db.commit()

        return True

    @staticmethod
    async def update_configuration(
        db: AsyncSession,
        client_id: uuid.UUID,
        source_id: uuid.UUID,
        data: SourceConfigurationUpdate,
    ) -> SourceConfigurationModel | None:
        """Update scraping configuration for a source."""

        source = await SourceService.get_by_id(
            db=db,
            client_id=client_id,
            source_id=source_id,
        )

        if not source:
            return None

        # Because get_by_id() now uses selectinload(),
        # source.configuration is already loaded.
        configuration = source.configuration

        if not configuration:
            configuration = SourceConfigurationModel(
                source_id=source.id,
            )
            db.add(configuration)

        update_data = data.model_dump(
            exclude_unset=True
        )

        for field, value in update_data.items():
            setattr(configuration, field, value)

        await db.commit()

        await db.refresh(configuration)

        return configuration


    @staticmethod
    def before_collection(source: SourceModel) -> bool:
        """Return whether a source is currently eligible for collection."""
        if not source.is_active:
            return False
        if source.circuit_state != CircuitStateEnum.OPEN:
            return True

        opened = source.circuit_opened_at
        if opened is None:
            source.circuit_state = CircuitStateEnum.HALF_OPEN
            source.circuit_half_opened_at = datetime.now(timezone.utc)
            return True

        elapsed = (datetime.now(timezone.utc) - opened).total_seconds()
        if elapsed >= 300:
            source.circuit_state = CircuitStateEnum.HALF_OPEN
            source.circuit_half_opened_at = datetime.now(timezone.utc)
            source.health_status = HealthStatusEnum.WARNING
            return True

        return False

    @staticmethod
    def record_collection_success(source: SourceModel) -> None:
        source.failure_count = 0
        source.consecutive_successes += 1
        source.circuit_state = CircuitStateEnum.CLOSED
        source.circuit_opened_at = None
        source.circuit_half_opened_at = None
        source.health_status = HealthStatusEnum.HEALTHY

    @staticmethod
    def record_collection_failure(source: SourceModel) -> bool:
        """Record a failure and return True exactly when the circuit transitions to OPEN."""
        source.failure_count += 1
        source.consecutive_successes = 0
        tripped = source.failure_count >= 5 and source.circuit_state != CircuitStateEnum.OPEN
        if tripped:
            source.circuit_state = CircuitStateEnum.OPEN
            source.circuit_opened_at = datetime.now(timezone.utc)
            source.circuit_half_opened_at = None
            source.health_status = HealthStatusEnum.FAILING
        else:
            source.health_status = HealthStatusEnum.WARNING
        return tripped
