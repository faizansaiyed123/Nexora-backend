"""
Offering API routes.
"""

import uuid
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import (
    AuthenticatedUserContext,
    get_current_user_claims,
    require_admin,
    require_analyst,
)
from backend.db.session import get_db
from backend.schemas.offering import (
    OfferingCreate,
    OfferingRead,
    OfferingUpdate,
)
from backend.services.offering_service import OfferingService


router = APIRouter()


@router.post(
    "",
    response_model=OfferingRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_offering(
    data: OfferingCreate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new offering for the authenticated client."""

    return await OfferingService.create(
        db=db,
        client_id=auth_ctx.client_id,
        data=data,
    )


@router.get(
    "",
    response_model=List[OfferingRead],
    dependencies=[Depends(require_analyst)],
)
async def list_offerings(
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all offerings belonging to the authenticated client."""

    return await OfferingService.list(
        db=db,
        client_id=auth_ctx.client_id,
    )


@router.get(
    "/{offering_id}",
    response_model=OfferingRead,
    dependencies=[Depends(require_analyst)],
)
async def get_offering(
    offering_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a single offering belonging to the authenticated client."""

    offering = await OfferingService.get_by_id(
        db=db,
        client_id=auth_ctx.client_id,
        offering_id=offering_id,
    )

    if not offering:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering not found.",
        )

    return offering


@router.patch(
    "/{offering_id}",
    response_model=OfferingRead,
    dependencies=[Depends(require_admin)],
)
async def update_offering(
    offering_id: uuid.UUID,
    data: OfferingUpdate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update an offering belonging to the authenticated client."""

    offering = await OfferingService.update(
        db=db,
        client_id=auth_ctx.client_id,
        offering_id=offering_id,
        data=data,
    )

    if not offering:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering not found.",
        )

    return offering


@router.delete(
    "/{offering_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_offering(
    offering_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete an offering belonging to the authenticated client."""

    deleted = await OfferingService.delete(
        db=db,
        client_id=auth_ctx.client_id,
        offering_id=offering_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering not found.",
        )

    return None
