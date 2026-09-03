"""
Business logic for Sources.

All source operations are tenant-scoped through the competitor's client_id.
"""

import uuid
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.exceptions import ForbiddenException
from backend.models.competitor import (
    CompetitorModel,
    SourceConfigurationModel,
    SourceModel,
)
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

        # Create source.
        source = SourceModel(
            competitor_id=data.competitor_id,
            name=data.name,
            base_url=data.base_url,
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
