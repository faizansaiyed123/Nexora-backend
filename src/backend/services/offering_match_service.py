"""
Business logic for Offering Matches.

Connects a client offering to a specific competitor source URL.
All operations are tenant-scoped through the offering's client_id.
"""

import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.competitor import (
    CompetitorModel,
    OfferingMatchModel,
    SourceModel,
)
from backend.models.offering import OfferingModel
from backend.schemas.offering_match import (
    OfferingMatchCreate,
    OfferingMatchUpdate,
)


class OfferingMatchService:
    """Service layer for offering-to-competitor matching."""

    @staticmethod
    async def create(
        db: AsyncSession,
        client_id: uuid.UUID,
        data: OfferingMatchCreate,
    ) -> OfferingMatchModel:
        """
        Create a match between one client offering and
        one competitor source URL.

        Ensures that both the offering and source belong
        to the authenticated client.
        """

        # Verify offering belongs to this client.
        offering_result = await db.execute(
            select(OfferingModel).where(
                OfferingModel.id == data.offering_id,
                OfferingModel.client_id == client_id,
            )
        )

        offering = offering_result.scalar_one_or_none()

        if not offering:
            raise ValueError("Offering not found.")

        # Verify source belongs to a competitor owned by this client.
        source_result = await db.execute(
            select(SourceModel)
            .join(
                CompetitorModel,
                SourceModel.competitor_id == CompetitorModel.id,
            )
            .where(
                SourceModel.id == data.source_id,
                CompetitorModel.client_id == client_id,
            )
        )

        source = source_result.scalar_one_or_none()

        if not source:
            raise ValueError("Source not found.")

        # Check duplicate match.
        existing_result = await db.execute(
            select(OfferingMatchModel).where(
                OfferingMatchModel.offering_id == data.offering_id,
                OfferingMatchModel.source_id == data.source_id,
                OfferingMatchModel.target_url == str(data.target_url),
            )
        )

        existing = existing_result.scalar_one_or_none()

        if existing:
            raise ValueError(
                "This offering is already matched to this source URL."
            )

        match = OfferingMatchModel(
            offering_id=data.offering_id,
            source_id=data.source_id,
            target_url=str(data.target_url),
            match_status=data.match_status,
            confidence_score=data.confidence_score,
            extraction_selectors=data.extraction_selectors,
            is_active=data.is_active,
        )

        db.add(match)
        await db.commit()
        await db.refresh(match)

        return match

    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        client_id: uuid.UUID,
        match_id: uuid.UUID,
    ) -> Optional[OfferingMatchModel]:
        """
        Get one offering match while enforcing tenant isolation.
        """

        result = await db.execute(
            select(OfferingMatchModel)
            .join(
                OfferingModel,
                OfferingMatchModel.offering_id == OfferingModel.id,
            )
            .where(
                OfferingMatchModel.id == match_id,
                OfferingModel.client_id == client_id,
            )
        )

        return result.scalar_one_or_none()

    @staticmethod
    async def list(
        db: AsyncSession,
        client_id: uuid.UUID,
    ) -> List[OfferingMatchModel]:
        """Return all matches belonging to the authenticated client."""

        result = await db.execute(
            select(OfferingMatchModel)
            .join(
                OfferingModel,
                OfferingMatchModel.offering_id == OfferingModel.id,
            )
            .where(
                OfferingModel.client_id == client_id,
            )
            .order_by(OfferingMatchModel.created_at.desc())
        )

        return list(result.scalars().all())

    @staticmethod
    async def update(
        db: AsyncSession,
        client_id: uuid.UUID,
        match_id: uuid.UUID,
        data: OfferingMatchUpdate,
    ) -> Optional[OfferingMatchModel]:
        """Update a client-owned offering match."""

        match = await OfferingMatchService.get_by_id(
            db=db,
            client_id=client_id,
            match_id=match_id,
        )

        if not match:
            return None

        update_data = data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            if field == "target_url":
                value = str(value)

            setattr(match, field, value)

        await db.commit()
        await db.refresh(match)

        return match

    @staticmethod
    async def delete(
        db: AsyncSession,
        client_id: uuid.UUID,
        match_id: uuid.UUID,
    ) -> bool:
        """Delete a client-owned offering match."""

        match = await OfferingMatchService.get_by_id(
            db=db,
            client_id=client_id,
            match_id=match_id,
        )

        if not match:
            return False

        await db.delete(match)
        await db.commit()

        return True
