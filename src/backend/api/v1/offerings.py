"""
Offering & Dynamic Catalog API routes.
Implements the complete RESTful interface for client catalog management:
- Single CRUD (Create, Read, Update, Soft Archive)
- Search, Multi-field Filtering & Pagination
- Monitoring Toggle
- Bulk Ingestion (up to 1,000 items) with index-attributed error feedback
- Bulk Soft Archive (up to 1,000 items)
- Dynamic Field Definitions CRUD
- Catalog Export (CSV flattened attributes & JSON)
- Strict Tenant Isolation & RBAC enforcement
"""

from decimal import Decimal
from typing import Annotated, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import (
    AuthenticatedUserContext,
    get_current_user_claims,
    require_admin,
    require_analyst,
    require_reader,
)
from backend.db.session import get_db
from backend.models.enums import OfferingTypeEnum
from backend.schemas.offering import (
    BulkOfferingArchiveRequest,
    BulkOfferingArchiveResponse,
    BulkOfferingImportRequest,
    BulkOfferingImportResponse,
    DynamicFieldDefinitionCreate,
    DynamicFieldDefinitionRead,
    OfferingCreate,
    OfferingDetailRead,
    OfferingHistoryRead,
    OfferingPaginationResponse,
    OfferingRead,
    OfferingUpdate,
    ToggleMonitoringResponse,
)
from backend.services.offering_service import OfferingService

router = APIRouter()


# ============================================================================
# Dynamic Field Definitions Management
# ============================================================================

@router.get(
    "/fields",
    response_model=List[DynamicFieldDefinitionRead],
    summary="List Dynamic Field Definitions",
    dependencies=[Depends(require_reader)],
)
async def list_dynamic_fields(
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Retrieves all auto-discovered and user-configured dynamic field schema definitions
    for the authenticated client organization.
    """
    return await OfferingService.list_dynamic_fields(
        db=db,
        client_id=auth_ctx.client_id,
    )


@router.post(
    "/fields",
    response_model=DynamicFieldDefinitionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create or Update Dynamic Field Definition",
    dependencies=[Depends(require_admin)],
)
async def create_or_update_dynamic_field(
    data: DynamicFieldDefinitionCreate,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Registers or updates a dynamic field definition for catalog attributes.
    Requires ADMIN or OWNER role.
    """
    return await OfferingService.create_dynamic_field(
        db=db,
        client_id=auth_ctx.client_id,
        data=data,
    )


@router.delete(
    "/fields/{field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Dynamic Field Definition",
    dependencies=[Depends(require_admin)],
)
async def delete_dynamic_field(
    field_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Deletes a dynamic field definition. Requires ADMIN or OWNER role.
    """
    deleted = await OfferingService.delete_dynamic_field(
        db=db,
        client_id=auth_ctx.client_id,
        field_id=field_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dynamic field definition not found.",
        )
    return None


# ============================================================================
# Bulk Operations & Catalog Export
# ============================================================================

@router.post(
    "/bulk",
    response_model=BulkOfferingImportResponse,
    status_code=status.HTTP_200_OK,
    summary="Bulk Ingest Offerings",
    dependencies=[Depends(require_admin)],
)
async def bulk_ingest_offerings(
    data: BulkOfferingImportRequest,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Bulk uploads up to 1,000 offerings in a single API request.
    Performs SSRF security checks, JSONB attribute sanitization, dynamic field auto-discovery,
    and returns per-item error breakdown for any failed rows.
    """
    return await OfferingService.bulk_create(
        db=db,
        client_id=auth_ctx.client_id,
        request=data,
    )


@router.post(
    "/bulk-archive",
    response_model=BulkOfferingArchiveResponse,
    status_code=status.HTTP_200_OK,
    summary="Bulk Soft-Archive Offerings",
    dependencies=[Depends(require_admin)],
)
async def bulk_archive_offerings(
    data: BulkOfferingArchiveRequest,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Soft-archives up to 1,000 offerings at once for the authenticated organization.
    Preserves all historical price match intelligence and snapshots.
    """
    return await OfferingService.bulk_archive(
        db=db,
        client_id=auth_ctx.client_id,
        request=data,
    )


@router.get(
    "/export",
    summary="Export Catalog (CSV or JSON)",
    dependencies=[Depends(require_reader)],
)
async def export_catalog(
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    format: str = Query(
        default="csv",
        description="Export format ('csv' or 'json')",
        pattern=r"^(csv|json)$",
    ),
    include_archived: bool = Query(
        default=False,
        description="Whether to include archived offerings in the export",
    ),
):
    """
    Exports the entire client catalog with flattened dynamic attribute columns (CSV)
    or nested JSON structure.
    """
    content, media_type, filename = await OfferingService.export_catalog(
        db=db,
        client_id=auth_ctx.client_id,
        format_type=format,
        include_archived=include_archived,
    )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )


# ============================================================================
# Core Offering CRUD & Monitoring
# ============================================================================

@router.post(
    "",
    response_model=OfferingRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create Offering",
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
    """
    Creates a new offering for the authenticated client.
    Enforces SSRF prevention, dynamic schema registration, and catalog quota limits.
    """
    return await OfferingService.create(
        db=db,
        client_id=auth_ctx.client_id,
        data=data,
    )


@router.get(
    "",
    response_model=OfferingPaginationResponse,
    summary="List & Filter Offerings",
    dependencies=[Depends(require_reader)],
)
async def list_offerings(
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Optional[str] = Query(
        default=None,
        description="Search term (matches name, SKU, category, or description)",
    ),
    offering_type: Optional[OfferingTypeEnum] = Query(
        default=None,
        description="Filter by offering type",
    ),
    category: Optional[str] = Query(
        default=None,
        description="Filter by category substring",
    ),
    market: Optional[str] = Query(
        default=None,
        max_length=2,
        description="Filter by 2-letter ISO market code (e.g. US, UK, DE)",
    ),
    currency: Optional[str] = Query(
        default=None,
        max_length=3,
        description="Filter by 3-letter currency code (e.g. USD, EUR, GBP)",
    ),
    min_price: Optional[Decimal] = Query(
        default=None,
        ge=0,
        description="Minimum current price",
    ),
    max_price: Optional[Decimal] = Query(
        default=None,
        ge=0,
        description="Maximum current price",
    ),
    is_monitored: Optional[bool] = Query(
        default=None,
        description="Filter by active monitoring status",
    ),
    include_archived: bool = Query(
        default=False,
        description="Whether to include soft-archived offerings",
    ),
    page: int = Query(
        default=1,
        ge=1,
        description="Page number (1-indexed)",
    ),
    page_size: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Items per page (max 500)",
    ),
):
    """
    Lists offerings with multi-attribute filtering, search, pagination,
    and real-time competitor match metrics.
    """
    return await OfferingService.list_offerings(
        db=db,
        client_id=auth_ctx.client_id,
        q=q,
        offering_type=offering_type,
        category=category,
        market=market,
        currency=currency,
        min_price=min_price,
        max_price=max_price,
        is_monitored=is_monitored,
        include_archived=include_archived,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{offering_id}/history",
    response_model=List[OfferingHistoryRead],
    summary="Get Offering Price History",
    dependencies=[Depends(require_analyst)],
)
async def offering_history(
    offering_id: uuid.UUID,
    auth_ctx: Annotated[AuthenticatedUserContext, Depends(get_current_user_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await OfferingService.history(
        db=db,
        client_id=auth_ctx.client_id,
        offering_id=offering_id,
    )


@router.get(
    "/{offering_id}",
    response_model=OfferingDetailRead,
    summary="Get Offering Details",
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
    """
    Retrieves full details of a specific offering, including matched competitor sources,
    live snapshot prices, and price delta analytics.
    """
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
    summary="Update Offering",
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
    """
    Partially updates an offering. Dynamic attributes are safely merged and re-validated.
    """
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
    summary="Soft-Archive Offering",
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
    """
    Soft-archives an offering (sets is_archived = true).
    Preserves all historical price snapshots and competitor matches.
    """
    archived = await OfferingService.soft_archive(
        db=db,
        client_id=auth_ctx.client_id,
        offering_id=offering_id,
    )

    if not archived:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offering not found.",
        )

    return None


@router.post(
    "/{offering_id}/toggle-monitoring",
    response_model=ToggleMonitoringResponse,
    summary="Toggle Offering Monitoring",
    dependencies=[Depends(require_admin)],
)
async def toggle_offering_monitoring(
    offering_id: uuid.UUID,
    auth_ctx: Annotated[
        AuthenticatedUserContext,
        Depends(get_current_user_claims),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Enables or disables automated price collection & monitoring for an offering.
    """
    return await OfferingService.toggle_monitoring(
        db=db,
        client_id=auth_ctx.client_id,
        offering_id=offering_id,
    )
