"""
Competitor API routes.
"""

import uuid
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_db
from backend.schemas.competitor import (
    CompetitorCreate,
    CompetitorRead,
    CompetitorUpdate,
)
from backend.services.competitor_service import CompetitorService
from backend.core.deps import (
    AuthenticatedUserContext,
    get_current_user_claims,
    require_admin,
    require_analyst,
)

router = APIRouter()


@router.post(
    "",
    response_model=CompetitorRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_competitor(
    data: CompetitorCreate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new competitor for the authenticated client."""

    return await CompetitorService.create(
        db=db,
        client_id=auth_ctx.client_id,
        data=data,
    )


@router.get(
    "",
    response_model=List[CompetitorRead],
    dependencies=[Depends(require_analyst)],
)
async def list_competitors(
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all competitors belonging to the authenticated client."""

    return await CompetitorService.list(
        db=db,
        client_id=auth_ctx.client_id,
    )


@router.get(
    "/{competitor_id}",
    response_model=CompetitorRead,
    dependencies=[Depends(require_analyst)],
)
async def get_competitor(
    competitor_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a single competitor belonging to the authenticated client."""

    competitor = await CompetitorService.get_by_id(
        db=db,
        client_id=auth_ctx.client_id,
        competitor_id=competitor_id,
    )

    if not competitor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Competitor not found.",
        )

    return competitor


@router.patch(
    "/{competitor_id}",
    response_model=CompetitorRead,
    dependencies=[Depends(require_admin)],
)
async def update_competitor(
    competitor_id: uuid.UUID,
    data: CompetitorUpdate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update a competitor belonging to the authenticated client."""

    competitor = await CompetitorService.update(
        db=db,
        client_id=auth_ctx.client_id,
        competitor_id=competitor_id,
        data=data,
    )

    if not competitor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Competitor not found.",
        )

    return competitor


@router.delete(
    "/{competitor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_competitor(
    competitor_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a competitor belonging to the authenticated client."""

    deleted = await CompetitorService.delete(
        db=db,
        client_id=auth_ctx.client_id,
        competitor_id=competitor_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Competitor not found.",
        )

    return None
