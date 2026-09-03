"""
Business logic for Offerings.

All offering operations are tenant-scoped using client_id.
"""

import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.offering import OfferingModel
from backend.schemas.offering import OfferingCreate, OfferingUpdate


class OfferingService:
    """Service layer for offering management."""

    @staticmethod
    async def create(
        db: AsyncSession,
        client_id: uuid.UUID,
        data: OfferingCreate,
    ) -> OfferingModel:
        offering = OfferingModel(
            client_id=client_id,
            name=data.name,
            sku=data.sku,
            brand=data.brand,
            category_path=data.category_path,
            offering_type=data.offering_type,
            base_price=data.base_price,
            currency=data.currency,
            dynamic_attributes=data.dynamic_attributes,
            created_via=data.created_via,
        )

        db.add(offering)
        await db.commit()
        await db.refresh(offering)

        return offering

    @staticmethod
    async def get_by_id(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
    ) -> Optional[OfferingModel]:
        result = await db.execute(
            select(OfferingModel).where(
                OfferingModel.id == offering_id,
                OfferingModel.client_id == client_id,
            )
        )

        return result.scalar_one_or_none()

    @staticmethod
    async def list(
        db: AsyncSession,
        client_id: uuid.UUID,
    ) -> List[OfferingModel]:
        result = await db.execute(
            select(OfferingModel)
            .where(OfferingModel.client_id == client_id)
            .order_by(OfferingModel.created_at.desc())
        )

        return list(result.scalars().all())

    @staticmethod
    async def update(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
        data: OfferingUpdate,
    ) -> Optional[OfferingModel]:
        offering = await OfferingService.get_by_id(
            db=db,
            client_id=client_id,
            offering_id=offering_id,
        )

        if not offering:
            return None

        update_data = data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(offering, field, value)

        await db.commit()
        await db.refresh(offering)

        return offering

    @staticmethod
    async def delete(
        db: AsyncSession,
        client_id: uuid.UUID,
        offering_id: uuid.UUID,
    ) -> bool:
        offering = await OfferingService.get_by_id(
            db=db,
            client_id=client_id,
            offering_id=offering_id,
        )

        if not offering:
            return False

        await db.delete(offering)
        await db.commit()

        return True
