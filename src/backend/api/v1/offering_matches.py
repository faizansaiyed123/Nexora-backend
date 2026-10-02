"""
Offering Match API routes.

Connects client offerings to specific competitor source URLs.
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
    require_reader,
)
from backend.db.session import get_db
from backend.schemas.offering_match import (
    OfferingMatchCreate,
    OfferingMatchRead,
    OfferingMatchUpdate,
)
from backend.services.offering_match_service import OfferingMatchService


router = APIRouter()


@router.post(
    "",
    response_model=OfferingMatchRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_offering_match(
    data: OfferingMatchCreate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a match between an offering and competitor source URL."""

    try:
        return await OfferingMatchService.create(
            db=db,
            client_id=auth_ctx.client_id,
            data=data,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.get(
    "",
    response_model=List[OfferingMatchRead],
    dependencies=[Depends(require_reader)],
)
async def list_offering_matches(
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all offering matches for the authenticated client."""

    return await OfferingMatchService.list(
        db=db,
        client_id=auth_ctx.client_id,
    )


@router.get(
    "/{match_id}",
    response_model=OfferingMatchRead,
    dependencies=[Depends(require_analyst)],
)
async def get_offering_match(
    match_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get one offering match."""

    match = await OfferingMatchService.get_by_id(
        db=db,
        client_id=auth_ctx.client_id,
        match_id=match_id,
    )

    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering match not found.",
        )

    return match


@router.patch(
    "/{match_id}",
    response_model=OfferingMatchRead,
    dependencies=[Depends(require_admin)],
)
async def update_offering_match(
    match_id: uuid.UUID,
    data: OfferingMatchUpdate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update an offering match."""

    match = await OfferingMatchService.update(
        db=db,
        client_id=auth_ctx.client_id,
        match_id=match_id,
        data=data,
    )

    if not match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering match not found.",
        )

    return match


@router.delete(
    "/{match_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_offering_match(
    match_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete an offering match."""

    deleted = await OfferingMatchService.delete(
        db=db,
        client_id=auth_ctx.client_id,
        match_id=match_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering match not found.",
        )

    return None
