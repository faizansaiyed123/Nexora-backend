"""
Pydantic schemas for Crawler Execution Jobs, Observations, Snapshots, and Time-Series Analytics.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import (
    AvailabilityStatusEnum,
    ErrorCategoryEnum,
    JobStatusEnum,
    JobTypeEnum,
)


class JobBase(BaseModel):
    job_type: JobTypeEnum = Field(default=JobTypeEnum.SCHEDULED_CRAWL)


class JobCreate(JobBase):
    source_id: uuid.UUID
    meta_info: Dict[str, Any] = Field(default_factory=dict)


class JobRead(JobBase):
    id: uuid.UUID
    source_id: uuid.UUID
    status: JobStatusEnum
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_items_processed: int
    successful_items: int
    failed_items: int
    error_category: Optional[ErrorCategoryEnum] = None
    error_message: Optional[str] = None
    meta_info: Dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ObservationIngest(BaseModel):
    offering_match_id: uuid.UUID
    job_id: Optional[uuid.UUID] = None
    observed_price: Optional[Decimal] = Field(None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    availability: AvailabilityStatusEnum = Field(default=AvailabilityStatusEnum.IN_STOCK)
    response_time_ms: int = Field(default=0, ge=0)
    http_status_code: int = Field(default=200, ge=100, le=599)
    raw_payload: Optional[str] = None
    extracted_attributes: Dict[str, Any] = Field(default_factory=dict)


class ObservationRead(BaseModel):
    id: uuid.UUID
    offering_match_id: uuid.UUID
    job_id: Optional[uuid.UUID] = None
    observed_price: Optional[Decimal] = None
    currency: str
    availability: AvailabilityStatusEnum
    response_time_ms: int
    http_status_code: int
    extracted_attributes: Dict[str, Any]
    observed_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SnapshotRead(BaseModel):
    id: uuid.UUID
    offering_match_id: uuid.UUID
    current_price: Optional[Decimal] = None
    previous_price: Optional[Decimal] = None
    price_difference: Optional[Decimal] = None
    percentage_difference: Optional[float] = None
    current_availability: AvailabilityStatusEnum
    last_observed_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PriceHistoryPoint(BaseModel):
    observed_at: datetime
    price: Optional[Decimal]
    availability: AvailabilityStatusEnum


class PriceTrendResponse(BaseModel):
    offering_match_id: uuid.UUID
    current_price: Optional[Decimal]
    min_price: Optional[Decimal]
    max_price: Optional[Decimal]
    average_price: Optional[Decimal]
    history: List[PriceHistoryPoint]


class ObservationHistoryItem(BaseModel):
    id: uuid.UUID
    offering_match_id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_name: str
    source_id: uuid.UUID
    source_name: str
    target_url: str
    observed_at: datetime
    observed_price: Optional[Decimal] = None
    currency: str
    availability: AvailabilityStatusEnum
    response_time_ms: int
    http_status_code: int


class ObservationHistoryResponse(BaseModel):
    items: List[ObservationHistoryItem]
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_more: bool
