"""
Business logic for Competitors.

All competitor operations are tenant-scoped using client_id.
This prevents users from accessing competitors belonging to another client.
"""

import uuid
from typing import List
from backend.core.exceptions import ForbiddenException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import ForbiddenException
from backend.models.client import ClientModel
from backend.models.competitor import CompetitorModel
from backend.schemas.competitor import (
    CompetitorCreate,
    CompetitorUpdate,
)


class CompetitorService:
    """Service layer for competitor management."""

    @staticmethod
    async def create(
        db: AsyncSession,
        client_id: uuid.UUID,
        data: CompetitorCreate,
    ) -> CompetitorModel:
        """
        Create a competitor for the authenticated client.

        Also enforces the client's max_competitors limit.
        """

        # Get the client so we can enforce its limits.
        client_result = await db.execute(
            select(ClientModel).where(ClientModel.id == client_id)
        )
        client = client_result.scalar_one_or_none()

        if not client:
            raise ForbiddenException(
                "Client account not found.",
                code="CLIENT_NOT_FOUND",
            )

        # Check competitor limit.
        count_result = await db.execute(
            select(func.count(CompetitorModel.id)).where(
                CompetitorModel.client_id == client_id
            )
        )
        competitor_count = count_result.scalar_one()

        if competitor_count >= client.max_competitors:
            raise ForbiddenException(
                f"Competitor limit reached. Your plan allows "
                f"{client.max_competitors} competitors.",
                code="COMPETITOR_LIMIT_REACHED",
            )

        # Check for duplicate domain within this client.
        existing_result = await db.execute(
            select(CompetitorModel).where(
                CompetitorModel.client_id == client_id,
                CompetitorModel.domain == data.domain,
            )
        )
        existing = existing_result.scalar_one_or_none()

        if existing:
            raise ForbiddenException(
                "A competitor with this domain already exists.",
                code="COMPETITOR_ALREADY_EXISTS",
            )

        competitor = CompetitorModel(
            client_id=client_id,
            name=data.name,
            domain=data.domain,
            status=data.status,
        )

        db.add(competitor)
        await db.commit()
        await db.refresh(competitor)

        return competitor

    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        client_id: uuid.UUID,
        competitor_id: uuid.UUID,
    ) -> CompetitorModel | None:
        """
        Get one competitor belonging to the authenticated client.

        The client_id condition is mandatory for tenant isolation.
        """

        result = await db.execute(
            select(CompetitorModel).where(
                CompetitorModel.id == competitor_id,
                CompetitorModel.client_id == client_id,
            )
        )

        return result.scalar_one_or_none()

    @staticmethod
    async def list(
        db: AsyncSession,
        client_id: uuid.UUID,
    ) -> List[CompetitorModel]:
        """Return all competitors belonging to the authenticated client."""

        result = await db.execute(
            select(CompetitorModel)
            .where(CompetitorModel.client_id == client_id)
            .order_by(CompetitorModel.created_at.desc())
        )

        return list(result.scalars().all())

    @staticmethod
    async def update(
        db: AsyncSession,
        client_id: uuid.UUID,
        competitor_id: uuid.UUID,
        data: CompetitorUpdate,
    ) -> CompetitorModel | None:
        """
        Update a competitor belonging to the authenticated client.
        """

        competitor = await CompetitorService.get_by_id(
            db=db,
            client_id=client_id,
            competitor_id=competitor_id,
        )

        if not competitor:
            return None

        update_data = data.model_dump(exclude_unset=True)

        # If domain is being changed, make sure the new domain
        # doesn't already belong to another competitor in this client.
        if "domain" in update_data and update_data["domain"] != competitor.domain:
            existing_result = await db.execute(
                select(CompetitorModel).where(
                    CompetitorModel.client_id == client_id,
                    CompetitorModel.domain == update_data["domain"],
                    CompetitorModel.id != competitor_id,
                )
            )

            existing = existing_result.scalar_one_or_none()

            if existing:
                raise ForbiddenException(
                    "A competitor with this domain already exists.",
                    code="COMPETITOR_ALREADY_EXISTS",
                )

        for field, value in update_data.items():
            setattr(competitor, field, value)

        await db.commit()
        await db.refresh(competitor)

        return competitor

    @staticmethod
    async def delete(
        db: AsyncSession,
        client_id: uuid.UUID,
        competitor_id: uuid.UUID,
    ) -> bool:
        """
        Delete a competitor belonging to the authenticated client.

        Related sources are deleted through the SQLAlchemy cascade
        configured on CompetitorModel.sources.
        """

        competitor = await CompetitorService.get_by_id(
            db=db,
            client_id=client_id,
            competitor_id=competitor_id,
        )

        if not competitor:
            return False

        await db.delete(competitor)
        await db.commit()

        return True
