"""
Extraction & Collection Request / Response Schemas for Nexora.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import (
    AvailabilityStatusEnum,
    CollectionMethodEnum,
    ErrorCategoryEnum,
    JobStatusEnum,
    JobTypeEnum,
)


class CollectionRunRequest(BaseModel):
    """Payload for triggering an on-demand live collection run."""

    source_id: Optional[UUID] = Field(
        None,
        description="Monitored source UUID if pre-registered in DB",
    )
    url: Optional[str] = Field(
        None,
        description="Direct target URL to scrape dynamically",
    )
    offering_id: Optional[UUID] = Field(
        None,
        description="Client offering UUID to correlate",
    )
    competitor_offering_id: Optional[UUID] = Field(
        None,
        description="Competitor offering UUID to link",
    )
    client_id: Optional[UUID] = Field(
        None,
        description="Tenant client UUID",
    )
    force_browser: bool = Field(
        False,
        description="Force headless Playwright browser rendering",
    )
    timeout_seconds: int = Field(
        15,
        ge=2,
        le=60,
        description="HTTP / Browser fetch timeout in seconds",
    )


class CollectionResult(BaseModel):
    """
    Standardized result contract for all scraping and extraction engines.
    """

    http_status_code: Optional[int] = None
    final_url: Optional[str] = None
    response_time_ms: int = 0

    price: Optional[Decimal] = None
    currency: Optional[str] = None

    availability: AvailabilityStatusEnum = (
        AvailabilityStatusEnum.UNKNOWN
    )

    name: Optional[str] = None
    sku: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None

    attributes: Dict[str, Any] = Field(default_factory=dict)
    extracted_fields: Dict[str, Any] = Field(default_factory=dict)

    raw_content: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)

    collection_method: CollectionMethodEnum = (
        CollectionMethodEnum.HTTP_FAST
    )

    is_success: bool = False

    error_message: Optional[str] = None

    error_category: Optional[ErrorCategoryEnum] = (
        ErrorCategoryEnum.NONE
    )

    job_id: Optional[UUID] = None
    observation_id: Optional[UUID] = None
    snapshot_id: Optional[UUID] = None

    # Backwards-compatible aliases.
    @property
    def status_code(self) -> Optional[int]:
        return self.http_status_code

    @property
    def duration_ms(self) -> int:
        return self.response_time_ms

    @property
    def raw_response(self) -> Optional[str]:
        return self.raw_content

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        from_attributes=True,
    )


class CollectionRunResponse(BaseModel):
    """API response for POST /v1/collections/run."""

    status: JobStatusEnum
    job_id: UUID
    message: str
    result: CollectionResult


class JobDetailResponse(BaseModel):
    """Detailed audit job view with full telemetry."""

    id: UUID
    client_id: UUID

    source_id: Optional[UUID] = None
    offering_id: Optional[UUID] = None
    competitor_offering_id: Optional[UUID] = None

    job_type: JobTypeEnum
    status: JobStatusEnum
    collection_method: CollectionMethodEnum

    target_url: Optional[str] = None
    final_url: Optional[str] = None

    http_status_code: Optional[int] = None
    duration_ms: int = 0

    price: Optional[Decimal] = None
    currency: Optional[str] = None

    availability_status: AvailabilityStatusEnum

    error_category: ErrorCategoryEnum
    error_message: Optional[str] = None
    result_summary: Optional[str] = None

    raw_content: Optional[str] = None
    extracted_attributes: Dict[str, Any] = Field(
        default_factory=dict
    )

    scheduled_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
