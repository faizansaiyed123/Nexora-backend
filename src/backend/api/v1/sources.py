"""
Source API routes.
"""

import uuid
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import (
    AuthenticatedUserContext,
    get_current_user_claims,
    require_admin,
    require_analyst,
)
from backend.db.session import get_db
from backend.schemas.competitor import (
    SourceConfigurationRead,
    SourceConfigurationUpdate,
    SourceCreate,
    SourceRead,
    SourceUpdate,
)
from backend.services.source_service import SourceService


router = APIRouter()


@router.post(
    "",
    response_model=SourceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_source(
    data: SourceCreate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a source for a competitor belonging to the authenticated client."""

    return await SourceService.create(
        db=db,
        client_id=auth_ctx.client_id,
        data=data,
    )


@router.get(
    "",
    response_model=List[SourceRead],
    dependencies=[Depends(require_analyst)],
)
async def list_sources(
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    competitor_id: Optional[uuid.UUID] = Query(default=None),
):
    """List sources belonging to the authenticated client."""

    return await SourceService.list(
        db=db,
        client_id=auth_ctx.client_id,
        competitor_id=competitor_id,
    )


@router.get(
    "/{source_id}",
    response_model=SourceRead,
    dependencies=[Depends(require_analyst)],
)
async def get_source(
    source_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get one source belonging to the authenticated client."""

    source = await SourceService.get_by_id(
        db=db,
        client_id=auth_ctx.client_id,
        source_id=source_id,
    )

    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source not found.",
        )

    return source


@router.patch(
    "/{source_id}",
    response_model=SourceRead,
    dependencies=[Depends(require_admin)],
)
async def update_source(
    source_id: uuid.UUID,
    data: SourceUpdate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update a source belonging to the authenticated client."""

    source = await SourceService.update(
        db=db,
        client_id=auth_ctx.client_id,
        source_id=source_id,
        data=data,
    )

    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source not found.",
        )

    return source


@router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_source(
    source_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a source belonging to the authenticated client."""

    deleted = await SourceService.delete(
        db=db,
        client_id=auth_ctx.client_id,
        source_id=source_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source not found.",
        )

    return None


@router.patch(
    "/{source_id}/configuration",
    response_model=SourceConfigurationRead,
    dependencies=[Depends(require_admin)],
)
async def update_source_configuration(
    source_id: uuid.UUID,
    data: SourceConfigurationUpdate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update scraping configuration for a source."""

    configuration = await SourceService.update_configuration(
        db=db,
        client_id=auth_ctx.client_id,
        source_id=source_id,
        data=data,
    )

    if not configuration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source not found.",
        )

    return configuration
